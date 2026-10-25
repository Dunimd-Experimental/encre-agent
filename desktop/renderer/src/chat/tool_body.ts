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
 * Tool card body primitives.
 *
 * A tool card is a *strip header* plus a *body*. The header is identical for
 * every tool; only the body differs. So each body is an independently
 * reusable primitive and a tool never owns its rendering — it just declares
 * which body it uses:
 *
 *   write  = strip header + DiffBody
 *   bash   = strip header + TerminalBody
 *   web_search = strip header + WebResultsBody
 *
 * Bodies are registered into the `chat.tool.body` slot, so adding a new one is
 * a single registration instead of a new file or a new `if` branch:
 *
 *   registerToolBody({ id: "jupyter", kind: "jupyter", render: renderJupyter })
 *
 * Built-ins are registered with `owner: "builtin"` and can be overridden by
 * reusing their id, which is the same contract as every other UI slot.
 */

import type { ToolCallState } from "../core/types.js";
import { BUILTIN_OWNER, ext } from "../core/extensions.js";
import { t } from "../features/i18n.js";
import { renderDiffHtml } from "./diff_render.js";
import { escapeHtml } from "./chat_markdown.js";

export const TOOL_BODY_SLOT = "chat.tool.body";

/** Matches the context-collapse stub the backend injects into old tool results. */
const COLLAPSED_RESULT_RE = /^\[collapsed tool result:/;

/**
 * True when `result` is a collapse placeholder (not real tool output). Such
 * stubs must never be rendered verbatim: they can contain call ids and raw
 * preview bytes. Renderers return an empty body instead, and the strip
 * header keeps showing the command summary.
 */
export function isCollapsedPlaceholder(result: string): boolean {
  return typeof result === "string" && COLLAPSED_RESULT_RE.test(result);
}

/** Which body renders a tool's result. */
export type ToolBodyKind =
  | "diff"
  | "terminal"
  | "web-results"
  | "web-fetched"
  | "pre";

/** Everything a body renderer may read about the tool call. */
export interface ToolBodyContext {
  tc: ToolCallState;
  result: string;
  /** Free-form scalars from the tool's UI declaration (`meta.props`). */
  props?: Record<string, string | number | boolean>;
}

/** One registered body renderer. */
export type ToolBodySpec = {
  id: string;
  kind: ToolBodyKind;
  owner?: string;
  priority?: number;
  title?: string;
  render: (result: string, ctx: ToolBodyContext) => string;
};

// === Registry ===============================================================

/** Registers (or replaces) a body renderer and returns an unregister function. */
export function registerToolBody<T extends ToolBodySpec = ToolBodySpec>(spec: T): () => void {
  return ext.register<T>(TOOL_BODY_SLOT, spec);
}

export function unregisterToolBody(id: string): boolean {
  return ext.unregister(TOOL_BODY_SLOT, id);
}

/** Every registered body, priority-descending. */
export function listToolBodies(): ToolBodySpec[] {
  return ext.entries<ToolBodySpec>(TOOL_BODY_SLOT);
}

/** Highest-priority renderer registered for `kind`. */
export function resolveToolBody(ctx: ToolBodyContext, kind: ToolBodyKind): ToolBodySpec | null {
  ensureToolBodies();
  for (const spec of ext.entries<ToolBodySpec>(TOOL_BODY_SLOT)) {
    if (spec.kind !== kind) continue;
    return spec;
  }
  return null;
}

/**
 * Renders `kind` against the call's result. Never throws and never returns
 * undefined — an unregistered or broken renderer simply produces no body.
 */
export function renderToolBody(ctx: ToolBodyContext, kind: ToolBodyKind): string {
  const spec = resolveToolBody(ctx, kind);
  if (!spec) return "";
  try {
    return spec.render(ctx.result, ctx) ?? "";
  } catch {
    return "";
  }
}

// === DiffBody ===============================================================

/**
 * Filename announced on the first result line of the file tools:
 * "Successfully wrote N chars to <path>" / "Applied N edit(s) to <path>."
 */
export function diffFileName(result: string): string {
  const firstLine = result.split("\n")[0] || "";
  const pathMatch = firstLine.match(/(?:to|to:) (.+?)\.?\s*$/);
  return pathMatch ? pathMatch[1].trim() : "";
}

/**
 * Pulls the ```diff fence out of a file-mutation result and renders it with the
 * shared diff renderer, so the chat card and the review panel stay identical.
 */
export function renderDiffBody(result: string): string {
  const fileName = diffFileName(result);
  const diffMatch = result.match(/```diff\n([\s\S]*?)```/);
  if (!diffMatch) return "";
  return renderDiffHtml(diffMatch[1].trim(), fileName);
}

let _builtinsRegistered = false;

/** Registers the built-in bodies once; safe to call repeatedly. */
export function ensureToolBodies(): void {
  if (_builtinsRegistered) return;
  _builtinsRegistered = true;
  registerToolBody({
    id: "diff",
    owner: BUILTIN_OWNER,
    kind: "diff",
    title: "Unified diff",
    render: renderDiffBody,
  });
  registerToolBody({
    id: "terminal",
    owner: BUILTIN_OWNER,
    kind: "terminal",
    title: "Command output",
    render: renderTerminalBody,
  });
  registerToolBody({
    id: "web-results",
    owner: BUILTIN_OWNER,
    kind: "web-results",
    title: "Search results",
    render: renderWebResults,
  });
  registerToolBody({
    id: "web-fetched",
    owner: BUILTIN_OWNER,
    kind: "web-fetched",
    title: "Fetched page",
    render: renderWebFetchedContent,
  });
  registerToolBody({
    id: "pre",
    owner: BUILTIN_OWNER,
    kind: "pre",
    title: "Truncated output",
    render: renderPreBody,
  });
}

// === TerminalBody ===========================================================

/**
 * Parses the JSON envelope terminal tools return and shows stdout/stderr.
 *
 * Returns "" when the result is not JSON so the caller can fall through to
 * {@link renderPreBody} and still show the raw text — the previous inline
 * implementation did exactly that with a `catch`, and a tool whose output is
 * not a valid envelope must not render an empty body.
 */
export function renderTerminalBody(result: string): string {
  if (isCollapsedPlaceholder(result)) return "";
  try {
    const json = JSON.parse(result);
    const parts: string[] = [];
    if (json.stdout) parts.push(json.stdout);
    if (json.stderr) parts.push(json.stderr);
    const text = parts.join("\n");
    if (!text.trim()) return `<pre style="font-size:11.5px;color:var(--text-muted);margin:0;padding:0">${escapeHtml("(no output)")}</pre>`;
    const lines = text.split("\n");
    const maxLines = 20;
    const shown = lines.slice(0, maxLines);
    const hasMore = lines.length > maxLines;
    let html = `<pre style="font-size:11.5px;color:var(--text-secondary);white-space:pre-wrap;margin:0;line-height:1.5">${escapeHtml(shown.join("\n"))}`;
    if (hasMore) {
      html += `\n<span style="color:var(--text-muted)">${t("chat.moreLines", { count: lines.length - maxLines })}</span>`;
    }
    html += "</pre>";
    return html;
  } catch {
    // Not JSON — plain text, let the caller fall through.
    return "";
  }
}

// === PreBody ================================================================

/**
 * True when *result* is a terminal JSON envelope (an object carrying
 * `stdout`/`stderr`). Used by the generic pre body so a missing or stale
 * terminal declaration can never dump the envelope's internals (exit code,
 * timings, truncation flags) into the UI.
 */
export function isTerminalEnvelope(result: string): boolean {
  const trimmed = result.trim();
  if (!trimmed.startsWith("{")) return false;
  try {
    const json = JSON.parse(trimmed);
    return typeof json === "object" && json !== null && ("stdout" in json || "stderr" in json);
  } catch {
    return false;
  }
}

/** Plain output, trimmed of blank lines and capped, for the common case. */
export function renderPreBody(result: string): string {
  if (isCollapsedPlaceholder(result)) return "";
  // A terminal envelope must show only its output, never the raw JSON —
  // route it through the terminal renderer even when the declaration that
  // selects that renderer is missing (third-party tools, stale history).
  if (isTerminalEnvelope(result)) return renderTerminalBody(result);
  const lines = result.trim().split("\n").filter((l) => l.trim());
  const maxLines = 8;
  const shown = lines.slice(0, maxLines);
  const hasMore = lines.length > maxLines;
  let html = `<pre style="font-size:11.5px;color:var(--text-secondary);white-space:pre-wrap;margin:0;line-height:1.5">${escapeHtml(shown.join("\n"))}`;
  if (hasMore) {
    html += `\n<span style="color:var(--text-muted)">${t("chat.moreLines", { count: lines.length - maxLines })}</span>`;
  }
  html += "</pre>";
  return html;
}

// === WebResultsBody =========================================================

export function renderWebResults(result: string): string {
  const lines = result.trim().split("\n");
  // Detect format: markdown links "1. [title](url)" or raw text
  const hasMarkdownLinks = /\[.+?\]\(https?:\/\/.+?\)/.test(result);

  let html = '<div class="web-list">';
  let num = 1;

  if (hasMarkdownLinks) {
    // Parse markdown format: "1. [title](url)\n   snippet"
    const entries = splitMarkdownEntries(result);
    if (entries.length > 0) {
      for (const entry of entries) {
        html += renderWebEntry(num, entry.title, entry.url, entry.snippet);
        num++;
      }
    }
  } else {
    // Fallback: try key-value line parsing, or show as clean text
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      // Try "title - url - snippet" pattern
      const parts = trimmed.split(/\s{2,}-\s{2,}/);
      if (parts.length >= 2) {
        const title = parts[0].trim();
        const rest = parts.slice(1).join(" - ").trim();
        const urlMatch = rest.match(/^(https?:\/\/\S+)(?:\s+(.+))?$/);
        const url = urlMatch ? urlMatch[1] : "";
        const snip = urlMatch && urlMatch[2] ? urlMatch[2] : (url ? "" : rest);
        html += renderWebEntry(num, title, url, snip);
        num++;
      } else {
        // Catch inline URLs
        const urlMatch = trimmed.match(/^(.*?)\s*(https?:\/\/\S+)\s*(.*)$/);
        if (urlMatch) {
          html += renderWebEntry(num, urlMatch[1] || trimmed, urlMatch[2], urlMatch[3]);
          num++;
        } else {
          html += `<div class="web-item"><span class="web-num">${num}</span><span class="web-snip">${escapeHtml(trimmed)}</span></div>`;
          num++;
        }
      }
    }
  }

  if (num === 1) {
    // No structured entries found, show cleaned text
    const cleaned = result.replace(/\n{3,}/g, "\n\n").trim();
    html += `<div class="web-item" style="white-space:pre-wrap;line-height:1.5;font-size:11.5px">${escapeHtml(cleaned)}</div>`;
  }
  html += "</div>";
  return html;
}

/** Split web_search markdown output into title/url/snippet entries. */
export function splitMarkdownEntries(text: string): { title: string; url: string; snippet: string }[] {
  const entries: { title: string; url: string; snippet: string }[] = [];
  // Match: optional number. [title](url) followed by optional snippet on next line(s)
  const pattern = /(?:^\d+\.\s*)?\[(.+?)\]\((https?:\/\/[^)]+)\)\s*([\s\S]*?)(?=(?:^\d+\.\s*)?\[.+?\]\(https?:\/\/|$)/gm;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    entries.push({
      title: match[1].trim(),
      url: match[2].trim(),
      snippet: match[3].trim().replace(/\n+/g, " ").slice(0, 300),
    });
  }
  return entries;
}

export function renderWebEntry(num: number, title: string, url: string, snippet: string): string {
  const displayTitle = title || url || "(untitled)";
  // Clickable title if URL present, otherwise plain text
  const titleHtml = url
    ? `<a class="web-title" href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(displayTitle)}</a>`
    : `<span class="web-title">${escapeHtml(displayTitle)}</span>`;
  const shortUrl = url ? url.replace(/^https?:\/\//, "").replace(/\/$/, "").slice(0, 50) : "";
  const urlHtml = shortUrl ? `<span class="web-url">${escapeHtml(shortUrl)}</span>` : "";
  const snipHtml = snippet ? `<span class="web-snip">${escapeHtml(snippet)}</span>` : "";
  return `<div class="web-item">
    <span class="web-num">${num}</span>
    ${titleHtml}
    ${urlHtml}
    ${snipHtml}
  </div>`;
}

// === WebFetchedBody =========================================================

/** Render web_fetch results: clean scraped text into readable format. */
export function renderWebFetchedContent(result: string): string {
  const lines = result.split("\n").map(l => l.trim()).filter(l => l);
  // Deduplicate consecutive near-identical lines (common in scraped content)
  const deduped: string[] = [];
  for (const line of lines) {
    const prev = deduped[deduped.length - 1];
    if (prev && _lineSimilarity(prev, line) > 0.85) continue;
    if (line.length < 3 && deduped.length > 0) continue; // skip single-char noise
    deduped.push(line);
  }
  // Limit display
  const maxLines = 60;
  const shown = deduped.slice(0, maxLines);
  const more = deduped.length > maxLines
    ? `\n<span style="color:var(--text-muted)">… ${deduped.length - maxLines} more lines</span>`
    : "";
  return `<pre class="web-fetched-content">${escapeHtml(shown.join("\n"))}${more}</pre>`;
}

export function _lineSimilarity(a: string, b: string): number {
  if (a === b) return 1;
  const shorter = a.length < b.length ? a : b;
  const longer = a.length < b.length ? b : a;
  if (shorter.length === 0) return 0;
  let matches = 0;
  const words = new Set(shorter.split(/\s+/));
  for (const w of longer.split(/\s+/)) {
    if (words.has(w)) matches++;
  }
  return matches / Math.max(words.size, 1);
}
