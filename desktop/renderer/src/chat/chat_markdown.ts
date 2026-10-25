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

// linkify-it treats scheme-less tokens like `refines.md` or `查看记忆.md` as
// web domains because `.md` (and a few other source/doc extensions) are also
// real country-code TLDs, and non-ASCII names get punycode-encoded into
// `http://xn--.../`. Those are almost always file references the model cites
// in its answer, not URLs, so reject them from auto-linking. Real websites
// users paste normally carry an explicit scheme and stay untouched.
const FILE_LIKE_TLDS = new Set(["md", "sh", "rs", "so", "st", "json", "yaml", "yml", "txt", "log"]);
md.linkify.set({
  validate: (link: { raw?: string; text?: string }) => {
    const text = (link.text || link.raw || "").trim();
    // Non-ASCII hostnames (e.g. CJK file names) punycode into xn-- domains.
    if (/[^\x00-\x7F]/.test(text)) return false;
    const m = /^(?:[a-z0-9-]+\.)+([a-z0-9-]+)$/i.exec(text.replace(/^www\./i, ""));
    if (!m) return true;
    return !FILE_LIKE_TLDS.has(m[1].toLowerCase());
  },
});

/**
 * The info string after a fence is written by the model, not by us: it can be
 * ```Python, ```py title="x", ```{.js}, or localised (```命令). Resolve it to a
 * highlight.js grammar id when one exists, otherwise keep the raw token for
 * display -- label comes from the info string, never from a generic fallback.
 */
const LANG_ALIASES: Record<string, string> = {
  js: "javascript", node: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", tsx: "typescript",
  py: "python", python3: "python", gyp: "python",
  sh: "bash", zsh: "bash", fish: "bash", console: "bash", terminal: "bash", cmd: "bash", ps1: "powershell",
  yml: "yaml", md: "markdown", mkd: "markdown",
  htm: "xml", html: "xml", svg: "xml", xhtml: "xml", vue: "xml",
  rs: "rust", golang: "go", kt: "kotlin", kts: "kotlin",
  rb: "ruby", "c++": "cpp", cc: "cpp", hpp: "cpp", hxx: "cpp",
  cs: "csharp", "c#": "csharp", objc: "objectivec", "objective-c": "objectivec",
  yamlscript: "yaml", dockerfile: "bash", make: "makefile", mk: "makefile",
  toml: "ini", conf: "ini", env: "ini", properties: "ini",
  text: "plaintext", txt: "plaintext", plain: "plaintext", none: "plaintext",
  jsonc: "json", json5: "json", jsonl: "json",
  sql: "sql", mysql: "sql", postgres: "sql", postgresql: "sql", psql: "sql",
};

function resolveFenceLang(info: string): { label: string; grammar: string } {
  // ```{.js} / ```js title="a.js" / ```JS  ->  first bare word
  const first = info
    .replace(/^\{\s*\.?/, "")
    .replace(/\}\s*$/, "")
    .trim()
    .split(/[\s,:]/)[0]
    .replace(/^language-/, "");
  if (!first) return { label: "", grammar: "" };
  const lower = first.toLowerCase();
  const grammar = LANG_ALIASES[lower] ?? lower;
  if (hljs.getLanguage(grammar)) return { label: first, grammar };
  // Unknown grammar (e.g. a localised name): still label the block with it,
  // just render it unhighlighted instead of pretending it is "code".
  return { label: first, grammar: "" };
}

md.renderer.rules.fence = (tokens: any[], idx: number) => {
  const token = tokens[idx];
  const content: string = token.content;
  const { label, grammar } = resolveFenceLang(token.info ? String(token.info) : "");
  let highlighted = "";

  if (grammar) {
    try {
      highlighted = hljs.highlight(content, { language: grammar }).value;
    } catch {
      highlighted = escapeHtml(content);
    }
  } else {
    highlighted = escapeHtml(content);
  }

  const attr = grammar ? ` class="hljs language-${grammar}"` : ' class="hljs"';
  const langLabel = label ? escapeHtml(label) : t("general.code");
  const codeAttr = `data-code="${escapeHtml(content)}"`;

  // Same copy affordance as the message action bar: a lucide icon button with
  // the shared `.copy-flash` checkmark feedback (see chat_actions.ts).
  return `<div class="code-block-wrapper">
    <div class="code-block-header">
      <span class="code-lang">${langLabel}</span>
      <button class="btn-icon btn-icon--msg code-copy" type="button" data-tooltip="${escapeHtml(t("chat.copy"))}" ${codeAttr}><i data-lucide="copy" class="lucide lucide-sm"></i></button>
    </div>
    <pre><code${attr}>${highlighted}</code></pre>
  </div>\n`;
};

md.renderer.rules.code_inline = (tokens: any[], idx: number) => {
  const content = tokens[idx].content;
  return `<code class="inline-code">${escapeHtml(content)}</code>`;
};

/** Per-render counter for deduplicating heading anchor ids. */
let _slugCounts: Map<string, number> = new Map();

/**
 * GitHub-style heading slug, CJK-safe: "## 部署指南 (Deploy)" -> "部署指南-deploy".
 * CJK characters are kept as-is; only ASCII/CJK punctuation and whitespace are
 * normalized so anchors like `[x](#部署指南)` survive.
 */
export function slugifyHeading(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[\s\u3000]+/g, "-")
    .replace(/[!"#$%&'()*+,./:;<=>?@[\\\]^`{|}~，。：；！？、（）《》“”‘’【】·—…]/g, "")
    .replace(/^-+|-+$/g, "");
}

// Headings get stable `id` anchors so intra-document links can scroll to them
// (markdown-it's default renderer emits no ids at all, which is why every
// markdown view in the app silently ignored `[x](#section)` links).
const defaultHeadingOpen = md.renderer.rules.heading_open;
md.renderer.rules.heading_open = (tokens: any[], idx: number, opts: any, env: any, self: any) => {
  const inline = tokens[idx + 1];
  const base = slugifyHeading(inline?.content ?? "") || "section";
  const seen = _slugCounts.get(base) ?? 0;
  _slugCounts.set(base, seen + 1);
  tokens[idx].attrSet("id", seen > 0 ? `${base}-${seen}` : base);
  if (defaultHeadingOpen) {
    return defaultHeadingOpen(tokens, idx, opts, env, self);
  }
  return self.renderToken(tokens, idx, opts);
};

/** Renders markdown text to HTML (trimming trailing breaks/empty paragraphs). */
export function renderMarkdown(text: string): string {
  if (!text) return "";
  const trimmed = text.replace(/\n+$/, "");
  _slugCounts = new Map();
  const html = md.render(trimmed);
  return html.replace(/(?:<br\s*\/?>\s*)+$/, "").replace(/<p>\s*<\/p>\s*$/, "");
}

/**
 * Scrolls the nearest scrollable ancestor chain to the anchor target inside
 * `container` (heading id or named anchor).  Returns true when found.
 */
export function scrollToMarkdownAnchor(container: HTMLElement, anchor: string): boolean {
  if (!anchor) return false;
  let id = anchor;
  try {
    id = decodeURIComponent(anchor);
  } catch {
    // keep the encoded form
  }
  const safe = CSS.escape(id);
  const el = container.querySelector(`[id="${safe}"], a[name="${safe}"]`);
  if (!el) return false;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  return true;
}

export interface MarkdownLinkHandlers {
  /** Relative/local file links (no scheme), e.g. "other.md" or "img/a.png". */
  onOpenFile?: (href: string) => void;
  /** External links: http(s), www, mailto. */
  onOpenExternal?: (href: string) => void;
}

/**
 * Delegated click handling shared by every markdown-rendered view (E sidebar
 * preview, memory detail dialog, Encre Agent Desktop Subwindow (ESD) legal/docs).  Intra-document anchors
 * scroll in place -- the default navigation would just move the window hash
 * and visibly do nothing.  Returns true when the click was fully handled.
 */
export function handleMarkdownContentClick(
  container: HTMLElement,
  e: MouseEvent,
  handlers: MarkdownLinkHandlers
): boolean {
  const a = (e.target as HTMLElement).closest("a");
  if (!a) return false;
  const raw = a.getAttribute("href") || "";
  if (!raw) return false;
  if (/^(https?:\/\/|www\.|mailto:)/i.test(raw)) {
    e.preventDefault();
    handlers.onOpenExternal?.(raw);
    return true;
  }
  // Anchor part of the link: "#section" (self) or "file.md#section".
  // markdown-it percent-encodes non-ASCII hrefs (CJK file names), so decode
  // the path portion before matching/resolving it.
  const hashIdx = raw.indexOf("#");
  const path = safeDecode(hashIdx < 0 ? raw : raw.slice(0, hashIdx));
  const anchor = hashIdx >= 0 ? raw.slice(hashIdx + 1) : "";
  if (anchor && (!path || /\.(md|markdown|html?)$/i.test(path))) {
    e.preventDefault();
    // Self-anchor (no path, or the anchor target already exists in this
    // view): scroll in place.  Cross-file anchors open the target file --
    // the anchor itself is dropped, the consumer's viewer has no way to
    // pre-scroll before its own render.
    const selfAnchor = !path || container.querySelector(`[id="${CSS.escape(safeDecode(anchor))}"]`);
    if (selfAnchor) {
      scrollToMarkdownAnchor(container, anchor);
    } else {
      handlers.onOpenFile?.(path);
    }
    return true;
  }
  if (path && handlers.onOpenFile) {
    e.preventDefault();
    handlers.onOpenFile(path);
    return true;
  }
  return false;
}

/** decodeURIComponent that never throws. */
function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * Rewrites Windows-style backslash paths inside inline link/image
 * destinations to forward slashes BEFORE tokenization.  markdown-it's
 * destination parser strips backslash escapes, so `![x](D:\dir\pic.png)`
 * would reach the renderer as src="D:dirpic.png" and never resolve.  Only
 * destinations that contain a backslash AND end in a known file extension
 * are rewritten, keeping escape sequences and code examples intact.
 */
export function normalizeMarkdownWinPaths(text: string): string {
  return text.replace(
    /\]\(([^()\n]*\\[^()\n]*)\)/g,
    (m, dest: string) =>
      /\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|ogg|mp3|wav|pdf|md|markdown|txt|html?)\s*$/i.test(dest.trim())
        ? `](${dest.replace(/\\/g, "/")})`
        : m
  );
}

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
