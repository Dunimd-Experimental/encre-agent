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

import { insertChipsAtCaret, wireChipClose } from "../ui/chip.js";

/**
 * File & attachment handling for the composer.
 *
 * Owns the composer's attachment surface: drag-and-drop, paste and file/folder
 * pickers feed into base64-backed `AttachmentMeta` records stored in app state.
 * Attachments are rendered as read-only "chips" inserted at the caret or
 * appended to the input, and kept in sync with state via a subscription.
 */

import { getMaterialFileIcon, getMaterialFolderIcon, getVSIFileIcon } from "@baybreezy/file-extension-icon";
import { AttachmentMeta } from "../core/types.js";
import { addAttachments, clearAttachments, removeAttachment, getState, subscribe } from "../core/state.js";
import { t } from "./i18n.js";

const MAX_SINGLE_SIZE = 10 * 1024 * 1024;
const BATCH_SIZE = 5;

// ── Inline attachment placeholder grammar ────────────────────────────────
// A chip inside the composer serializes to `<attach n="name" />` at its
// exact position in the text.  The same grammar is used by the message
// bubble renderer (chip back at its inline position) and by the rollback
// editor (chip rebuilt where the placeholder sits), so a chip added mid
// sentence stays mid sentence through the whole round trip.

/** Matches an inline `<attach n="..." />` placeholder (the `c` count form
 *  emitted for attachment-only submits is tolerated for compatibility). */
export const ATTACH_PLACEHOLDER_RE = /<attach\s+n="([^"]*)"(?:\s+c="\d+")?\s*\/>/g;

/** Encodes a file name into the placeholder attribute value. */
export function encodeAttachName(name: string): string {
  return name.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

/** Decodes the placeholder attribute value back into the file name. */
export function decodeAttachName(raw: string): string {
  return raw.replace(/&quot;/g, '"').replace(/&amp;/g, "&");
}

/** Builds the inline placeholder for a chip at its current text position. */
export function attachPlaceholder(name: string): string {
  return `<attach n="${encodeAttachName(name)}" />`;
}

function readBatch<T, R>(items: T[], fn: (item: T) => Promise<R>, size: number): Promise<R[]> {
  const results: R[] = [];
  const run = async (): Promise<R[]> => {
    for (let i = 0; i < items.length; i += size) {
      const batch = items.slice(i, i + size);
      results.push(...await Promise.all(batch.map(fn)));
    }
    return results;
  };
  return run();
}

// ── File icon resolution ─────────────────────────────────────────────
// The Material set misses several common types (scala, perl, protobuf,
// diff, epub, …) and drops them all onto one shared generic gray "file"
// glyph (verified by decoding every returned SVG and hashing it).
// `getFileIcon` therefore resolves hybrid: Material first, the bundled
// vscode-icons set (VSI) for the holes, and semantic aliases for the
// few types no set covers directly.

/** Extensions with no Material glyph but a unique VSI one. */
const VSI_ICON_EXTS = new Set(["diff", "patch", "bat", "m"]);
/** Extensions missing from both sets that have a semantically close
 *  sibling inside the VSI set. */
const VSI_ALIAS_EXTS: Record<string, string> = { cmd: "bat", pm: "pl", jks: "p12", jceks: "p12" };
/** Extensions missing from both sets with no VSI sibling either; reuse a
 *  semantically close Material glyph (book format → pdf, disc image → png). */
const MATERIAL_ALIAS_EXTS: Record<string, string> = {
  epub: "pdf",
  mobi: "pdf",
  azw3: "pdf",
  icns: "png",
};

let _materialGenericIcon: string | null = null;
let _vsiGenericIcon: string | null = null;
const pureExtIconCache = new Map<string, string>();

/** Lowercased extension of `name`'s basename; "" for dotfiles and
 *  extensionless names. */
function fileExt(name: string): string {
  const base = name.split(/[/\\]/).pop() ?? name;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

/** The generic gray "file" glyph each set returns for unknown inputs. */
function materialGenericIcon(): string {
  return (_materialGenericIcon ??= getMaterialFileIcon("ZZZZ"));
}

function vsiGenericIcon(): string {
  return (_vsiGenericIcon ??= getVSIFileIcon("ZZZZ"));
}

/** Rewrites `name`'s basename extension to `aliasExt`. */
function withExtAlias(name: string, aliasExt: string): string {
  const base = name.split(/[/\\]/).pop() ?? name;
  const dot = base.lastIndexOf(".");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  return `${stem}.${aliasExt}`;
}

/** Returns a file icon SVG data URI. Material set first; vscode-icons for
 *  the types Material misses outright or lumps with the generic glyph;
 *  semantic aliases for the rest.  Pure per-extension branches (aliases,
 *  VSI direct) are memoized; the general branch may resolve from the full
 *  name (suffix matches like `*.actions.ts`), so it is never cached. */
export function getFileIcon(name: string): string {
  const ext = fileExt(name);
  if (!ext) return getMaterialFileIcon(name);
  const cached = pureExtIconCache.get(ext);
  if (cached !== undefined) return cached;
  if (VSI_ICON_EXTS.has(ext)) {
    const vsi = getVSIFileIcon(name);
    pureExtIconCache.set(ext, vsi);
    return vsi;
  }
  const vsiAlias = VSI_ALIAS_EXTS[ext];
  if (vsiAlias) {
    const aliased = getVSIFileIcon(withExtAlias(name, vsiAlias));
    pureExtIconCache.set(ext, aliased);
    return aliased;
  }
  const materialAlias = MATERIAL_ALIAS_EXTS[ext];
  if (materialAlias) {
    const aliased = getMaterialFileIcon(withExtAlias(name, materialAlias));
    pureExtIconCache.set(ext, aliased);
    return aliased;
  }
  const material = getMaterialFileIcon(name);
  if (material === materialGenericIcon()) {
    // Material had nothing specific; try vscode-icons before settling for
    // the shared gray glyph (scala, proto, dart, svelte, … land here).
    const vsi = getVSIFileIcon(name);
    if (vsi !== vsiGenericIcon()) return vsi;
  }
  return material;
}

/** Returns a folder icon SVG data URI from baybreezy Material icons. */
export function getFolderIcon(name: string, open?: boolean): string {
  return getMaterialFolderIcon(name, open);
}

/** Returns the canonical terminal icon SVG data URI used for every terminal
 *  chip / tab across the app: the unframed lucide `terminal` glyph — a
 *  chevron plus an underscore, no surrounding box. */
export function getTerminalIcon(): string {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"` +
    ` fill="none" stroke="#71717a" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">` +
    `<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Manages composer attachments (drag/drop, paste, picker) and their chips. */
export class Files {
  private input: HTMLElement;

  /**
   * Constructor: wires drag/drop/paste listeners and a state subscription.
   *
   * @param input - The content-editable composer element that hosts chips.
   */
  constructor(input: HTMLElement) {
    this.input = input;

    this.input.addEventListener("dragover", (e) => {
      e.preventDefault();
      this.input.classList.add("drag-over");
    });

    this.input.addEventListener("dragleave", () => {
      this.input.classList.remove("drag-over");
    });

    this.input.addEventListener("drop", async (e: DragEvent) => {
      e.preventDefault();
      this.input.classList.remove("drag-over");
      const files = e.dataTransfer?.files;
      if (files && files.length > 0) {
        await this.readDroppedFiles(files);
      }
    });

    this.input.addEventListener("paste", async (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (items) {
        const pasteFiles: File[] = [];
        for (const item of items) {
          if (item.kind === "file") {
            const file = item.getAsFile();
            if (file) pasteFiles.push(file);
          }
        }
        if (pasteFiles.length > 0) {
          e.preventDefault();
          await this.readDroppedFiles(pasteFiles);
        }
      }
    });

    subscribe(() => {
      const attachments = getState().attachments;
      const existingPaths = new Set<string>();
      this.input.querySelectorAll('[data-attach]').forEach(el => {
        const path = el.getAttribute('data-path');
        if (path) {
          if (attachments.some(a => a.path === path)) {
            existingPaths.add(path);
          } else {
            el.remove();
          }
        }
      });
      const toAdd: AttachmentMeta[] = [];
      for (const a of attachments) {
        if (!existingPaths.has(a.path)) {
          toAdd.push(a);
        }
      }
      if (toAdd.length > 0) {
        const frag = document.createDocumentFragment();
        for (const a of toAdd) {
          frag.appendChild(this.makeChip(a));
        }
        this.input.appendChild(frag);
        this.input.appendChild(document.createTextNode(""));
        if ((window as any).lucide) {
          (window as any).lucide.createIcons({ nodes: [this.input] });
        }
        const ph = document.getElementById("prompt-placeholder");
        if (ph) ph.classList.add("hidden");
      }
    }, ["attachments"]);
  }

  /** Opens the native file picker and reads the selected files. */
  async promptForFiles(): Promise<void> {
    if (!window.electronAPI) return;
    const paths = await window.electronAPI.pickFiles();
    if (paths.length === 0) return;
    await this.readPaths(paths);
  }

  /** Opens the native folder picker and attaches the chosen directory. */
  async promptForFolder(): Promise<void> {
    if (!window.electronAPI) return;
    const dirPath = await window.electronAPI.pickDirectory();
    if (!dirPath) return;
    await this.readDirectory(dirPath);
  }

  /** Reads dropped File objects, converting each to a base64 attachment. */
  private async readDroppedFiles(files: FileList | File[]): Promise<void> {
    const arr = Array.from(files).filter(f => f.size <= MAX_SINGLE_SIZE);
    const attachments = await readBatch(arr, async (file) => {
      const filePath = (file as any).path || file.name;
      const content = await this.fileToBase64(file);
      return {
        name: file.name,
        path: filePath,
        content,
        mime_type: file.type || "",
        size: file.size || 0,
        is_binary: false,
      } as AttachmentMeta;
    }, BATCH_SIZE);
    if (attachments.length === 0) return;
    addAttachments(attachments);
    this.batchInsertChips(attachments);
  }

  /** Reads files from absolute paths via the Electron bridge. */
  private async readPaths(paths: string[]): Promise<void> {
    const attachments = await readBatch(paths, async (fp) => {
      if (!window.electronAPI) return null;
      const result = await window.electronAPI.readFile(fp);
      if (!result || result.size === 0 || result.size > MAX_SINGLE_SIZE) return null;
      const name = fp.split(/[/\\]/).pop() ?? fp;
      return {
        name,
        path: fp,
        content: result.content,
        mime_type: result.mime_type || "",
        size: result.size,
        is_binary: result.is_binary,
      } as AttachmentMeta;
    }, BATCH_SIZE);
    const valid = attachments.filter(a => a !== null) as AttachmentMeta[];
    if (valid.length === 0) return;
    addAttachments(valid);
    this.batchInsertChips(valid);
  }

  /** Attaches a directory (represented as a special attachment) to the composer. */
  async readDirectory(dirPath: string): Promise<void> {
    const api = window.electronAPI;
    if (!api) return;
    const result = await api.readDirectory(dirPath);
    if (!result) return;
    const att: AttachmentMeta = {
      name: result.name,
      path: result.path,
      content: "",
      mime_type: "text/x-directory",
      size: 0,
      is_binary: false,
    };
    addAttachments([att]);
    this.batchInsertChips([att]);
  }

  private fileToBase64(file: File): Promise<string> {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result as string;
        const base64 = result.split(",")[1] || result;
        resolve(base64);
      };
      reader.readAsDataURL(file);
    });
  }

  private batchInsertChips(attachments: AttachmentMeta[]): void {
    try {
      const fragChips = attachments.map((a) => this.makeChip(a));
      // Inserts at the caret (or appends) with a trailing separator space so
      // the caret stays off the chip border — see ui/chip.ts.
      insertChipsAtCaret(this.input, fragChips);
      if ((window as any).lucide) {
        (window as any).lucide.createIcons({ nodes: [this.input] });
      }
    } catch (e) {
      console.error("[Files] batchInsertChips error:", e);
    }
  }

  /** Builds a read-only attachment chip element and wires its remove button. */
  private makeChip(att: AttachmentMeta): HTMLSpanElement {
    return buildAttachChip(att);
  }
}

/** Builds a read-only attachment chip element (composer + bubble + rollback
 *  editor all render the same visual).  Wires the remove button, which is
 *  inert outside the composer because `removeAttachment` finds nothing. */
export function buildAttachChip(att: AttachmentMeta): HTMLSpanElement {
  const chip = document.createElement("span");
  chip.contentEditable = "false";
  chip.className = "mode-chip";
  chip.setAttribute("data-attach", "true");
  chip.setAttribute("data-path", att.path);
  chip.setAttribute("data-name", att.name);
  const isDir = att.mime_type === "text/x-directory";
  const isTerminal = att.mime_type === "text/x-terminal";
  const label = att.name;
  const icon = isDir
    ? getFolderIcon(att.name)
    : isTerminal
      ? getTerminalIcon()
      : getFileIcon(att.name);
  const summary = isDir ? t("files.folderSummary")
    : isTerminal ? t("files.lineSummary", { count: att.size })
    : "";
  const iconHtml = icon.startsWith("data:")
    ? `<img src="${icon}" class="chip-icon" style="width:12px;height:12px;margin:0 2px 0 0">`
    : `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i>`;
  const summaryHtml = summary ? `<span class="mode-card-summary">${summary}</span>` : "";
  chip.innerHTML = `${iconHtml}<span class="mode-card-label">${label}</span>${summaryHtml}`;

  // Close affordance via shared delegation (lucide swaps <i class="chip-icon">
  // for an <svg>, killing listeners bound to the icon element).  Also covers
  // the <img class="chip-icon"> variant via closest().
  wireChipClose(chip, () => removeAttachment(att.path));

  return chip;
}

function fmtFileSize(bytes: number): string {
  if (!bytes || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Rebuilds composer content for the rollback editor: plain text goes back as
 * text nodes and every inline `<attach ... />` placeholder is replaced by a
 * real chip at that exact position.  Terminal chips have no path-backed
 * attachment and are restored as literal text, matching the previous
 * behaviour.  Rebuilt attachments are also pushed back into app state (the
 * Files subscription sees their chips already in the DOM and skips them).
 */
export function restoreComposerWithChips(
  input: HTMLElement,
  text: string,
  refs: { name: string; size: number; icon: string; path?: string; mime_type?: string }[] | undefined,
): void {
  input.textContent = "";
  const frag = document.createDocumentFragment();
  const atts: AttachmentMeta[] = [];
  const consumed = new Set<number>();
  let last = 0;
  ATTACH_PLACEHOLDER_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ATTACH_PLACEHOLDER_RE.exec(text)) !== null) {
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    const name = decodeAttachName(m[1]);
    const idx = refs ? refs.findIndex((r, i) => !consumed.has(i) && r.name === name) : -1;
    const ref = idx >= 0 ? refs![idx] : undefined;
    if (ref && ref.path && ref.mime_type !== "text/x-terminal") {
      consumed.add(idx);
      const att: AttachmentMeta = {
        name: ref.name,
        path: ref.path,
        content: "",
        mime_type: ref.mime_type || "",
        size: ref.size,
        is_binary: false,
      };
      atts.push(att);
      frag.appendChild(buildAttachChip(att));
    } else {
      // No restorable reference (terminal chip or refs unavailable): keep
      // the placeholder as literal text so nothing disappears.
      frag.appendChild(document.createTextNode(m[0]));
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  input.appendChild(frag);
  // Replace, never append: stale chips from an earlier rollback must not
  // survive into the restored composer, or the next text-only send would
  // silently carry the old attachment again (the ghost image chip bug).
  clearAttachments();
  if (atts.length > 0) addAttachments(atts);
}
