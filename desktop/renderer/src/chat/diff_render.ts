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
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * Shared unified-diff renderer.
 *
 * Used by three call sites so a diff looks identical everywhere:
 *   - chat tool cards (file_write / file_edit / apply_patch results)
 *   - review panel, git mode   (raw `git diff` output)
 *   - review panel, artifact mode (artifact diff_text)
 *
 * Handles standard `git diff` output (with `diff --git` / `+++` / `@@`
 * headers) as well as header-less diffs produced by the native `compute_diff`
 * (e.g. new-file artifacts whose body is all `+` lines with no `@@` hunk
 * header). The hunk activates on the first content line, so diffs without a
 * `@@` header still render instead of collapsing to `+0 -0`.
 *
 * Renders in GitHub-style inline or split view. Hunk headers (@@ lines)
 * are skipped. No +/- prefix characters — color alone indicates add/del.
 *
 * Optional rendering modes (passed via DiffRenderOptions):
 *   - maxLines:       truncate huge diffs after N content lines
 *   - wordDiff:       inline <del>/<ins> word-level highlighting
 *   - hideWhitespace: dim whitespace-only change lines
 *   - splitView:      two-column synchronized-scroll layout
 */

import { getFileIcon } from "../features/files.js";
import { t } from "../features/i18n.js";

// No cap on rendered rows. Diffs from file_write / file_edit carry the whole
// file, so any limit silently hides part of the change the user asked to see.
const MAX_RENDER_LINES = Number.POSITIVE_INFINITY;

// Counter for generating unique split-view scroll container IDs
let splitIdCounter = 0;

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** True for git diff meta/header lines that carry no diff body content. */
function isDiffMetaLine(line: string): boolean {
  return (
    line.startsWith("diff --git") ||
    line.startsWith("--- ") ||
    line.startsWith("index ") ||
    line.startsWith("old mode ") ||
    line.startsWith("new mode ") ||
    line.startsWith("new file mode ") ||
    line.startsWith("deleted file mode ") ||
    line.startsWith("similarity index ") ||
    line.startsWith("dissimilarity index ") ||
    line.startsWith("rename from ") ||
    line.startsWith("rename to ") ||
    line.startsWith("copy from ") ||
    line.startsWith("copy to ") ||
    line.startsWith("Binary files ") ||
    line.startsWith("GIT binary patch")
  );
}

/** A parsed logical diff line (after stripping the +/-/ space prefix). */
interface DiffLine {
  kind: "add" | "del" | "ctx" | "hunk" | "meta";
  text: string;
  ln: number;    // new-file line number for add/ctx lines
  oldLn: number; // old-file line number for del/ctx lines (split view)
}

/**
 * Measure a string in visual monospace columns. ASCII counts 1 per char,
 * CJK / full-width chars count 2, so `ch`-based width estimates stay
 * accurate for locale text. Used by both the inline and split renderers.
 */
function visualWidth(s: string): number {
  let w = 0;
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    // Full-width (East Asian) ranges: CJK, kana, hangul, fullwidth punct/forms.
    const wide =
      (code >= 0x1100 && code <= 0x115f) ||   // Hangul Jamo
      (code >= 0x2e80 && code <= 0xa4cf) ||   // CJK Radicals..Yi
      (code >= 0xac00 && code <= 0xd7a3) ||   // Hangul Syllables
      (code >= 0xf900 && code <= 0xfaff) ||   // CJK Compat Ideographs
      (code >= 0xfe30 && code <= 0xfe4f) ||   // CJK Compat Forms
      (code >= 0xff00 && code <= 0xff60) ||   // Fullwidth Forms (incl. fullwidth ASCII)
      (code >= 0xffe0 && code <= 0xffe6) ||   // Fullwidth Signs
      (code >= 0x3000 && code <= 0x303f) ||   // CJK Symbols/Punct
      (code >= 0x3040 && code <= 0x30ff) ||   // Hiragana/Katakana
      (code >= 0x3400 && code <= 0x4dbf) ||   // CJK Ext A
      (code >= 0x4e00 && code <= 0x9fff);     // CJK Unified
    w += wide ? 2 : 1;
  }
  return w;
}

/**
 * Expand a compact unified diff into a whole-file diff using the current
 * file content: unchanged lines between/around hunks are restored as
 * context so every line of the file is shown (adds/dels stay highlighted).
 * Returns synthesized unified-diff text consumable by `parseDiff`.
 */
export function expandDiffToFullFile(diffText: string, fileContent: string): string {
  const { fileName, lines } = parseDiff(diffText);
  const fileLines = fileContent.replace(/\r\n/g, "\n").replace(/\n+$/, "").split("\n");
  const rows: Array<{ t: "ctx" | "add" | "del"; text: string }> = [];
  let expected = 1;
  for (const dl of lines) {
    if (dl.kind === "del") { rows.push({ t: "del", text: dl.text }); continue; }
    if (dl.kind !== "add" && dl.kind !== "ctx") continue;
    const L = dl.ln;
    while (expected < L && expected <= fileLines.length) {
      rows.push({ t: "ctx", text: fileLines[expected - 1] });
      expected++;
    }
    rows.push({ t: dl.kind, text: dl.text });
    expected = Math.max(expected, L + 1);
  }
  for (; expected <= fileLines.length; expected++) {
    rows.push({ t: "ctx", text: fileLines[expected - 1] });
  }
  if (rows.length === 0) return diffText;
  const body = rows
    .map((r) => (r.t === "ctx" ? " " : r.t === "add" ? "+" : "-") + r.text)
    .join("\n");
  return `--- a/${fileName}\n+++ b/${fileName}\n@@ -1,${fileLines.length} +1,${fileLines.length} @@\n${body}`;
}

/** Parse raw unified-diff text into logical lines + file metadata. */
function parseDiff(diffText: string): {
  fileName: string;
  lines: DiffLine[];
} {
  const lines = diffText.split("\n");
  let fileName = "";
  const out: DiffLine[] = [];
  let newLn = 0;
  let oldLn = 0;
  // Diffs from the native `compute_diff` carry no `diff --git` / `@@` headers
  // at all — every line is already prefixed content. Starting in a hunk for
  // that shape is what keeps the leading unchanged lines from being dropped;
  // for real `git diff` text the first header opens the hunk instead.
  const headerless = !/^(?:diff --git |@@ |--- )/m.test(diffText);
  let inHunk = headerless;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].replace(/\t/g, "    ");
    // Trailing "" is the artifact of the final newline, not a content line.
    const isTrailingBlank = i === lines.length - 1 && line === "";
    if (headerless) {
      if (isTrailingBlank) continue;
      if (line.startsWith("+")) {
        newLn++;
        out.push({ kind: "add", text: line.slice(1), ln: newLn, oldLn: 0 });
      } else if (line.startsWith("-")) {
        oldLn++;
        out.push({ kind: "del", text: line.slice(1), ln: 0, oldLn });
      } else {
        newLn++;
        oldLn++;
        const text = line.startsWith(" ") ? line.slice(1) : line;
        out.push({ kind: "ctx", text, ln: newLn, oldLn });
      }
      continue;
    }
    if (line.startsWith("+++ ")) {
      if (!line.startsWith("+++ /dev/null")) {
        const m = line.match(/^\+\+\+ (?:[ab]\/)?(.+)$/);
        if (m) fileName = m[1];
      }
    } else if (line.startsWith("--- ")) {
      if (!fileName) {
        const m = line.match(/^--- (?:[ab]\/)?(.+)$/);
        if (m && m[1] !== "/dev/null") fileName = m[1];
      }
    } else if (isDiffMetaLine(line)) {
      continue;
    } else if (line.startsWith("@@")) {
      inHunk = true;
      // Capture both old (-) and new (+) start line numbers.
      const m = line.match(/@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?/);
      newLn = m ? parseInt(m[2], 10) - 1 : 0;
      oldLn = m ? parseInt(m[1], 10) - 1 : 0;
      // Hunk headers are now skipped during rendering — we still parse
      // to correctly track line numbers but do not push a hunk line object.
    } else if (line.startsWith("+")) {
      inHunk = true;
      newLn++;
      out.push({ kind: "add", text: line.slice(1), ln: newLn, oldLn: 0 });
    } else if (line.startsWith("-")) {
      inHunk = true;
      oldLn++;
      out.push({ kind: "del", text: line.slice(1), ln: 0, oldLn });
    } else if (inHunk) {
      newLn++;
      oldLn++;
      const text = line.startsWith(" ") ? line.slice(1) : line;
      out.push({ kind: "ctx", text, ln: newLn, oldLn });
    }
  }
  return { fileName, lines: out };
}

/** Split a line into word tokens (runs of non-space + whitespace kept). */
function tokenize(text: string): string[] {
  return text.match(/(\s+|\S+)/g) || [];
}

/** LCS-based word diff between two lines -> [{op:'eq'|'del'|'add', tok}]. */
function wordDiff(oldText: string, newText: string): Array<{ op: "eq" | "del" | "add"; tok: string }> {
  const a = tokenize(oldText);
  const b = tokenize(newText);
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const result: Array<{ op: "eq" | "del" | "add"; tok: string }> = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      result.push({ op: "eq", tok: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      result.push({ op: "del", tok: a[i] });
      i++;
    } else {
      result.push({ op: "add", tok: b[j] });
      j++;
    }
  }
  while (i < n) result.push({ op: "del", tok: a[i++] });
  while (j < m) result.push({ op: "add", tok: b[j++] });
  return result;
}

/** Render a token list as HTML with <del>/<ins> for word-diff. */
function renderTokens(tokens: Array<{ op: "eq" | "del" | "add"; tok: string }>): string {
  let html = "";
  for (const t of tokens) {
    if (t.op === "eq") {
      html += escapeHtml(t.tok);
    } else if (t.op === "del") {
      html += `<del>${escapeHtml(t.tok)}</del>`;
    } else {
      html += `<ins>${escapeHtml(t.tok)}</ins>`;
    }
  }
  return html || " ";
}

/** True when a line's content is all whitespace. */
function isWhitespaceOnly(text: string): boolean {
  return text.trim() === "";
}

export interface DiffRenderOptions {
  fileNameFallback?: string;
  /** Explicit file name shown in the diff header, overriding whatever
   *  parseDiff extracts from the diff text (use when the diff carries no
   *  parseable file header, e.g. artifact diffs). */
  fileName?: string;
  /** Truncate after this many content lines (default MAX_RENDER_LINES). */
  maxLines?: number;
  /** Inline word-level <del>/<ins> highlighting. */
  wordDiff?: boolean;
  /** Dim lines whose change is whitespace-only. */
  hideWhitespace?: boolean;
  /** Split (two-column) view: old on the left, new on the right. */
  splitView?: boolean;
  /** Optional formatter for the truncated-line notice. */
  truncatedNotice?: (hidden: number) => string;
}

/**
 * Render a unified diff as HTML.
 *
 * Accepts either an options object or (for backward compatibility) a plain
 * string used as fileNameFallback.
 *
 * @returns A `<div class="diff-container">…</div>` string, or `""` for empty
 *   input so callers can render their own empty state.
 */
export function renderDiffHtml(
  diffText: string,
  optsOrFallback?: DiffRenderOptions | string,
): string {
  if (!diffText.trim()) return "";

  const opts: DiffRenderOptions = typeof optsOrFallback === "string"
    ? { fileNameFallback: optsOrFallback }
    : (optsOrFallback ?? {});

  const maxLines = opts.maxLines ?? MAX_RENDER_LINES;
  const { fileName, lines } = parseDiff(diffText);
  const fileLabel = opts.fileName || fileName || opts.fileNameFallback || "";

  const adds = lines.filter((l) => l.kind === "add").length;
  const dels = lines.filter((l) => l.kind === "del").length;

  // Longest content line (in visual columns). Used to stretch every add/del
  // row to the right edge of the widest line so the red/green highlight
  // covers the entire row, even when a changed line is short or empty.
  // CJK / full-width chars occupy 2 monospace columns, so counting raw
  // characters under-estimates the width and leaves highlights and text
  // misaligned — always measure in visual columns instead.
  const maxTextLen = lines.reduce((m, l) =>
    (l.kind === "add" || l.kind === "del" || l.kind === "ctx") && visualWidth(l.text) > m
      ? visualWidth(l.text)
      : m, 0);

  // Split view: two-column (old | new) layout.
  if (opts.splitView) {
    return renderSplitView(fileLabel, lines, adds, dels, opts, maxTextLen);
  }

  // Inline view (default)
  let contentCount = 0;
  let truncated = 0;
  const bodyRows: string[] = [];
  // Track the previous rendered line numbers on both sides so hunk gaps
  // (e.g. jumping from line 64 to line 213) get a visible separator row
  // instead of silently continuing.
  let prevNew = 0;
  let prevOld = 0;
  const gapRow = (n: number) =>
    `<div class="diff-row diff-row-gap"><span class="diff-ln">&nbsp;</span><span class="diff-content">${escapeHtml(t("sessionInner.reviewSkippedLines", { n }))}</span></div>`;
  for (const dl of lines) {
    if (dl.kind === "hunk") continue; // skip hunk headers
    if (dl.kind === "add" || dl.kind === "del" || dl.kind === "ctx") {
      if (contentCount >= maxLines) {
        truncated++;
        continue;
      }
      contentCount++;
    }
    // Hunk-gap separator: when this line starts a new segment, show how
    // many lines were skipped in between (prefer the new-side numbers).
    const gap =
      dl.ln > 0 && prevNew > 0 && dl.ln > prevNew + 1
        ? dl.ln - prevNew - 1
        : dl.oldLn > 0 && prevOld > 0 && dl.oldLn > prevOld + 1
          ? dl.oldLn - prevOld - 1
          : 0;
    if (gap > 0) bodyRows.push(gapRow(gap));
    bodyRows.push(renderRow(dl, opts, maxTextLen));
    if (dl.ln > 0) prevNew = dl.ln;
    if (dl.oldLn > 0) prevOld = dl.oldLn;
  }
  if (truncated > 0) {
    const notice = opts.truncatedNotice
      ? opts.truncatedNotice(truncated)
      : t("sessionInner.reviewTruncated", { n: truncated });
    bodyRows.push(`<div class="diff-row diff-row-truncated"><span class="diff-ln">&nbsp;</span><span class="diff-content">${escapeHtml(notice)}</span></div>`);
  }

  return `<div class="diff-container">
      <div class="diff-header">
        <span class="diff-file-icon"><img src="${getFileIcon(fileLabel)}" class="icon" style="width:14px;height:14px"></span>
        <span class="diff-file-name">${escapeHtml(fileLabel)}</span>
        <span class="diff-stats"><span class="diff-add-stat">+${adds}</span><span class="diff-del-stat">-${dels}</span></span>
      </div>
      <div class="diff-body">${bodyRows.join("")}</div>
    </div>`;
}

/**
 * Stretch every diff row to the widest content line so the add/del highlight
 * covers the whole row — all the way to where the horizontal scrollbar can
 * reach, not just up to each line's own text.
 *
 * Exposes the widest line's visual width (in monospace `ch` columns) as a CSS
 * custom property on the row. `.diff-content` then uses it as its `min-width`,
 * so short rows are stretched to the same right edge as the longest row.
 * CJK / full-width chars count 2 columns (see `visualWidth`).
 */
function contentWidthStyle(maxTextLen: number): string {
  if (maxTextLen <= 0) return "";
  // Cap the stretch width. Whole-file diffs can contain pathological outlier
  // lines (minified bundles, data URIs); letting every row inherit that width
  // produces multi-megapixel rows whose backgrounds fail to paint at all
  // (highlights silently disappear). Sane code lines stay far below this.
  const w = Math.min(maxTextLen, 800);
  return ` style="--diff-minw:${w}ch"`;
}

/** Render one logical diff line as an HTML row (inline view). */
function renderRow(dl: DiffLine, opts: DiffRenderOptions, maxTextLen = 0): string {
  if (dl.kind === "hunk") return ""; // skipped
  const wsOnly = opts.hideWhitespace && isWhitespaceOnly(dl.text);
  const wsClass = wsOnly ? " diff-row-ws-only" : "";
  const minW = contentWidthStyle(maxTextLen);
  if (dl.kind === "add") {
    const content = opts.wordDiff
      ? `<ins>${escapeHtml(dl.text)}</ins>`
      : (escapeHtml(dl.text) || " ");
    return `<div class="diff-row diff-row-add${wsClass}"><span class="diff-ln">${dl.ln}</span><span class="diff-content"${minW}>${content}</span></div>`;
  }
  if (dl.kind === "del") {
    const content = opts.wordDiff
      ? `<del>${escapeHtml(dl.text)}</del>`
      : (escapeHtml(dl.text) || " ");
    return `<div class="diff-row diff-row-del${wsClass}"><span class="diff-ln">&nbsp;</span><span class="diff-content"${minW}>${content}</span></div>`;
  }
  // context
  return `<div class="diff-row${wsClass}"><span class="diff-ln">${dl.ln}</span><span class="diff-content"${minW}>${escapeHtml(dl.text) || " "}</span></div>`;
}

/**
 * For word-diff, re-render adjacent del+add block pairs with inline
 * token-level <del>/<ins>. This post-processes the parsed lines so paired
 * changes show what words actually moved, not whole-line highlights.
 */
export function renderDiffHtmlWordDiff(
  diffText: string,
  opts?: Omit<DiffRenderOptions, "wordDiff">,
): string {
  return renderDiffHtml(diffText, { ...opts, wordDiff: true });
}

/**
 * Split (two-column) view: old file on the left, new file on the right.
 * Each side has its own scroll container; scrolling one syncs the other.
 * Hunk headers are skipped entirely. No +/- prefix characters.
 */
function renderSplitView(
  fileLabel: string,
  lines: DiffLine[],
  adds: number,
  dels: number,
  opts: DiffRenderOptions,
  maxTextLen = 0,
): string {
  const maxLines = opts.maxLines ?? MAX_RENDER_LINES;
  let contentCount = 0;
  let truncated = 0;

  // Each side stretches its rows to that side's own widest line, so the
  // highlight always fills the row up to the scrollbar's reach on that side
  // without padding the other side with blank space.
  let leftMax = 0;
  let rightMax = 0;
  for (const dl of lines) {
    const w = visualWidth(dl.text);
    if (dl.kind === "ctx") {
      if (w > leftMax) leftMax = w;
      if (w > rightMax) rightMax = w;
    } else if (dl.kind === "del") {
      if (w > leftMax) leftMax = w;
    } else if (dl.kind === "add") {
      if (w > rightMax) rightMax = w;
    }
  }
  const leftMinW = contentWidthStyle(leftMax);
  const rightMinW = contentWidthStyle(rightMax);

  // Buffer pending dels/adds so they pair up top-to-bottom.
  let delQueue: DiffLine[] = [];
  let addQueue: DiffLine[] = [];

  // Build left (old) and right (new) side arrays
  const leftRows: string[] = [];
  const rightRows: string[] = [];

  function addRow(isDel: boolean, oldLn: number, newLn: number, text: string): void {
    if (contentCount >= maxLines) { truncated++; return; }
    contentCount++;
    const cls = isDel ? "diff-row-del" : "diff-row-add";
    const side = isDel ? "left" : "right";
    const lineNum = isDel ? oldLn : newLn;
    if (side === "left") {
      leftRows.push(`<div class="diff-row ${cls}"><span class="diff-ln">${lineNum || "&nbsp;"}</span><span class="diff-content"${leftMinW}>${escapeHtml(text) || " "}</span></div>`);
      rightRows.push(`<div class="diff-row diff-row-empty"><span class="diff-ln">&nbsp;</span><span class="diff-content"${rightMinW}>&nbsp;</span></div>`);
    } else {
      leftRows.push(`<div class="diff-row diff-row-empty"><span class="diff-ln">&nbsp;</span><span class="diff-content"${leftMinW}>&nbsp;</span></div>`);
      rightRows.push(`<div class="diff-row ${cls}"><span class="diff-ln">${lineNum || "&nbsp;"}</span><span class="diff-content"${rightMinW}>${escapeHtml(text) || " "}</span></div>`);
    }
  }

  function flushQueues(): void {
    const n = Math.max(delQueue.length, addQueue.length);
    for (let i = 0; i < n; i++) {
      const d = delQueue[i];
      const a = addQueue[i];
      if (d) {
        leftRows.push(`<div class="diff-row diff-row-del"><span class="diff-ln">${d.oldLn || "&nbsp;"}</span><span class="diff-content"${leftMinW}>${escapeHtml(d.text) || " "}</span></div>`);
      } else {
        leftRows.push(`<div class="diff-row diff-row-empty"><span class="diff-ln">&nbsp;</span><span class="diff-content"${leftMinW}>&nbsp;</span></div>`);
      }
      if (a) {
        rightRows.push(`<div class="diff-row diff-row-add"><span class="diff-ln">${a.ln || "&nbsp;"}</span><span class="diff-content"${rightMinW}>${escapeHtml(a.text) || " "}</span></div>`);
      } else {
        rightRows.push(`<div class="diff-row diff-row-empty"><span class="diff-ln">&nbsp;</span><span class="diff-content"${rightMinW}>&nbsp;</span></div>`);
      }
    }
    delQueue = [];
    addQueue = [];
  }

  for (const dl of lines) {
    if (dl.kind === "hunk") {
      flushQueues();
      continue; // skip hunk headers entirely
    }
    if (contentCount >= maxLines) {
      if (dl.kind !== "ctx") { truncated++; }
      continue;
    }
    if (dl.kind === "del") {
      delQueue.push(dl);
    } else if (dl.kind === "add") {
      addQueue.push(dl);
    } else {
      // ctx: flush pending changes, then render a paired context row.
      flushQueues();
      contentCount++;
      const leftCtxRow = `<div class="diff-row"><span class="diff-ln">${dl.oldLn}</span><span class="diff-content"${leftMinW}>${escapeHtml(dl.text) || " "}</span></div>`;
      const rightCtxRow = `<div class="diff-row"><span class="diff-ln">${dl.ln}</span><span class="diff-content"${rightMinW}>${escapeHtml(dl.text) || " "}</span></div>`;
      leftRows.push(leftCtxRow);
      rightRows.push(rightCtxRow);
    }
  }
  flushQueues();

  if (truncated > 0) {
    const notice = opts.truncatedNotice
      ? opts.truncatedNotice(truncated)
      : t("diff.truncated", { count: truncated });
    const tr = `<div class="diff-row diff-row-truncated"><span class="diff-ln">&nbsp;</span><span class="diff-content">${escapeHtml(notice)}</span></div>`;
    leftRows.push(tr);
    rightRows.push(tr);
  }

  const splitId = `spsc-${++splitIdCounter}`;

  const iconHtml = `<img src="${getFileIcon(fileLabel)}" class="icon" style="width:14px;height:14px">`;

  return `<div class="diff-container diff-split" data-split-id="${splitId}">
      <div class="diff-header">
        <span class="diff-file-icon">${iconHtml}</span>
        <span class="diff-file-name">${escapeHtml(fileLabel)}</span>
        <span class="diff-stats"><span class="diff-add-stat">+${adds}</span><span class="diff-del-stat">-${dels}</span></span>
      </div>
      <div class="diff-split-scroll-wrap">
        <div class="diff-split-scroll-side" data-side="left" data-split-group="${splitId}">
          ${leftRows.join("")}
        </div>
        <div class="diff-split-scroll-side" data-side="right" data-split-group="${splitId}">
          ${rightRows.join("")}
        </div>
      </div>
    </div>`;
}

/**
 * Set up synchronized scrolling for split-view diff containers.
 * Call this after inserting split-view HTML into the DOM.
 * Both sides scroll in sync vertically.
 */
export function setupSplitViewScrollSync(container: HTMLElement): void {
  const pairs = new Map<string, { left?: HTMLElement; right?: HTMLElement }>();
  container.querySelectorAll<HTMLElement>(".diff-split-scroll-side").forEach((el) => {
    const group = el.getAttribute("data-split-group");
    if (!group) return;
    if (!pairs.has(group)) pairs.set(group, {});
    const pair = pairs.get(group)!;
    const side = el.getAttribute("data-side");
    if (side === "left") pair.left = el;
    else if (side === "right") pair.right = el;
  });

  function syncSide(src: HTMLElement, target: HTMLElement): void {
    if ((target as any).__syncing) return;
    (target as any).__syncing = true;
    target.scrollTop = src.scrollTop;
    target.scrollLeft = src.scrollLeft;
    requestAnimationFrame(() => { (target as any).__syncing = false; });
  }

  for (const pair of pairs.values()) {
    if (pair.left && pair.right) {
      pair.left.addEventListener("scroll", () => syncSide(pair.left!, pair.right!));
      pair.right.addEventListener("scroll", () => syncSide(pair.right!, pair.left!));
    }
  }
}


