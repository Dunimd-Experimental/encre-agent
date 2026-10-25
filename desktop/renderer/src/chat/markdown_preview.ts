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

import { renderMarkdown, handleMarkdownContentClick, normalizeMarkdownWinPaths } from "./chat_markdown.js";
import { createLucideIcons, flashCopyButton } from "./chat_tool_display.js";
import { showToast } from "../core/state.js";
import { t } from "../features/i18n.js";

export interface MarkdownPreviewOptions {
  onOpenLink?: (url: string) => void;
}

export class MarkdownPreviewView {
  readonly container: HTMLElement;
  private _body: HTMLElement;
  private _raw = "";
  private _basePath = "";
  private _destroyed = false;
  private _onOpenLink?: (url: string) => void;

  constructor(container: HTMLElement, _title?: string, opts?: MarkdownPreviewOptions) {
    this.container = container;
    this._onOpenLink = opts?.onOpenLink;
    container.style.cssText = "display:flex;flex-direction:column;flex:1;min-height:0;height:100%;";
    container.innerHTML = `<div class="markdown-preview-body"></div>`;
    this._body = container.querySelector(".markdown-preview-body")!;
    this._body.addEventListener("click", (e) => this._onClick(e));
  }

  setContent(markdown: string, basePath?: string): void {
    this._raw = markdown;
    if (basePath !== undefined) this._basePath = basePath;
    this._render();
  }

  setTitle(_title: string): void {
    // title is shown in the tab bar
  }

  private _resolveImages(html: string): string {
    if (!this._basePath) return html;
    const dir = this._basePath.replace(/\\/g, "/").replace(/\/?$/, "/");
    return html.replace(/<img\s+[^>]*src="([^"]+)"[^>]*>/gi, (match, src: string) => {
      if (/^(https?:|data:|local:\/\/\/|file:\/\/|\/|#)/i.test(src)) return match;
      // markdown-it percent-encodes non-ASCII srcs (CJK file names) -- decode
      // before resolving, Chromium re-encodes on the wire and the local
      // protocol handler decodes again.
      let rel = src.replace(/\\/g, "/");
      try { rel = decodeURIComponent(rel); } catch { /* keep encoded */ }
      // Absolute Windows path (D:/...) -- a full filesystem location, never
      // join it onto the document's base directory.
      if (/^[a-zA-Z]:\//.test(rel)) {
        return match.replace(`src="${src}"`, `src="local:///${rel}"`);
      }
      const parts = dir.split("/").filter(Boolean);
      for (const seg of rel.split("/")) {
        if (seg === "..") { if (parts.length > 1) parts.pop(); }
        else if (seg !== "." && seg) parts.push(seg);
      }
      return match.replace(`src="${src}"`, `src="local:///${parts.join("/")}"`);
    });
  }

  private _onClick(e: MouseEvent): void {
    // Fenced code blocks render the same icon-button copy affordance as the
    // chat transcript; outside the chat view no delegate handles it, so the
    // preview copies (and flashes) on its own.
    const codeCopyBtn = (e.target as HTMLElement).closest(".code-copy") as HTMLElement | null;
    if (codeCopyBtn) {
      e.preventDefault();
      e.stopPropagation();
      navigator.clipboard
        .writeText(codeCopyBtn.getAttribute("data-code") || "")
        .then(() => flashCopyButton(codeCopyBtn))
        .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
      return;
    }
    // Unified link handling: "#section" anchors scroll in-place (the old code
    // explicitly ignored them, so TOC links did nothing), relative paths and
    // external URLs are routed to the owner via onOpenLink.
    handleMarkdownContentClick(this._body, e, {
      onOpenFile: (href) => this._onOpenLink?.(href),
      onOpenExternal: (href) => this._onOpenLink?.(href),
    });
  }

  private _render(): void {
    if (this._destroyed) return;
    this._body.innerHTML = renderMarkdown(normalizeMarkdownWinPaths(this._raw));
    this._body.innerHTML = this._resolveImages(this._body.innerHTML);
    // Lucide only swaps `[data-lucide]` placeholders it is pointed at; without
    // this pass the code-block copy buttons stay empty 0x0 shells.
    createLucideIcons(this._body);
  }

  destroy(): void {
    this._destroyed = true;
    this.container.innerHTML = "";
  }
}
