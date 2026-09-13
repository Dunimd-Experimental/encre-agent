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

import MarkdownIt from "markdown-it";
// Only the "common" language set is bundled eagerly: the full build ships ~190
// language definitions (~1.1 MB) into the entry chunk, i.e. a third of the
// whole boot bundle parsed before the first frame, for grammars a chat code
// block almost never uses.  lib/common covers the 36 languages that actually
// show up (js/ts/python/rust/go/java/c/cpp/bash/sql/json/yaml/xml/markdown/…)
// and is API-compatible with the full instance (getLanguage/highlight).
import hljs from "highlight.js/lib/common";
import { t } from "../features/i18n.js";

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true,
  breaks: true,
});

md.renderer.rules.fence = (tokens: any[], idx: number) => {
  const token = tokens[idx];
  const lang = token.info ? token.info.trim().split(/\s+/)[0] : "";
  let content = token.content;
  let highlighted = "";

  if (lang && hljs.getLanguage(lang)) {
    try {
      highlighted = hljs.highlight(content, { language: lang }).value;
    } catch {
      highlighted = escapeHtml(content);
    }
  } else {
    highlighted = escapeHtml(content);
  }

  const attr = lang ? ` class="hljs language-${lang}"` : ' class="hljs"';
  const langLabel = lang ? escapeHtml(lang) : t("general.code");
  const codeAttr = `data-code="${escapeHtml(content)}"`;

  return `<div class="code-block-wrapper">
    <div class="code-block-header">
      <span class="code-lang">${langLabel}</span>
      <button class="code-copy" ${codeAttr}>${t("chat.copy")}</button>
    </div>
    <pre><code${attr}>${highlighted}</code></pre>
  </div>\n`;
};

md.renderer.rules.code_inline = (tokens: any[], idx: number) => {
  const content = tokens[idx].content;
  return `<code class="inline-code">${escapeHtml(content)}</code>`;
};

/** Renders markdown text to HTML (trimming trailing breaks/empty paragraphs). */
export function renderMarkdown(text: string): string {
  if (!text) return "";
  const trimmed = text.replace(/\n+$/, "");
  const html = md.render(trimmed);
  return html.replace(/(?:<br\s*\/?>\s*)+$/, "").replace(/<p>\s*<\/p>\s*$/, "");
}

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
