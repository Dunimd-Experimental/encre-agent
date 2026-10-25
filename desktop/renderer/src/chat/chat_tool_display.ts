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

import type { ToolCallState } from "../core/types.js";
import { safeCreateIcons } from "../core/ui_boundary.js";
import { t } from "../features/i18n.js";
import { metaFor } from "./tool_meta.js";
import { renderDiffBody, renderToolBody, isCollapsedPlaceholder } from "./tool_body.js";
import { TransitionHelper } from "../ui/transition-helper.js";

// === Tool call text helpers =======================================================
//
// No tool is named in this file. Which card a call gets, what it is called and
// how its result is shown all come from `tool_meta.ts`, which resolves the
// backend-declared UI (`encre.tools.tool_ui`) against the renderer's six
// hardcoded file-mutation tools. This file only turns a resolved call into the
// short strings the cards display.


export function compactText(value: unknown, max = 88): string {
  if (value === undefined || value === null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > max ? `${oneLine.slice(0, max - 3)}...` : oneLine;
}

/**
 * Resolves the target file path of a file tool call.
 *
 * `tool_call_end` normally replaces `params` with the parsed argument object,
 * but every render between the first result and that event still sees the raw
 * `arguments` string — and history replay never re-sends `tool_call_end` at
 * all. In both cases `params.path` is missing, so the strip summary fell back
 * to dumping the tool result (i.e. the file's first lines). Parse defensively
 * here so the path is available regardless of how far the stream got.
 */
export function toolFilePath(tc: ToolCallState): string {
  const PATH_KEYS = ["file_path", "path", "filename", "filepath", "file", "target"];
  const direct = firstParam(tc, PATH_KEYS);
  if (direct) return direct;
  const raw = tc.params?.arguments;
  if (typeof raw === "string" && raw.trim().startsWith("{")) {
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      for (const key of PATH_KEYS) {
        const v = parsed[key];
        if (typeof v === "string" && v.trim()) return v;
      }
    } catch {
      // Partial JSON while the arguments are still streaming — ignore.
    }
  }
  return "";
}

/**
 * Trims a path to fit a strip summary without ever cutting off the file name,
 * which is the part that identifies what the tool actually touched.
 * Keeps `…/<parent>/<name>` when it fits, otherwise degrades to `…/<name>`.
 */
export function compactPath(p: string, max = 88): string {
  const path = (p || "").trim();
  if (!path) return "";
  if (path.length <= max) return path;
  const parts = path.split(/[\\/]+/).filter(Boolean);
  const name = parts[parts.length - 1] || path;
  if (parts.length >= 2) {
    const tail2 = `…/${parts[parts.length - 2]}/${name}`;
    if (tail2.length <= max) return tail2;
  }
  const tail1 = `…/${name}`;
  return tail1.length <= max ? tail1 : compactText(name, max);
}

/**
 * Try to extract a short human-readable summary from a JSON result string.
 * Returns null if the input is not valid JSON or no usable field is found.
 */
export function extractJsonSummary(result: string): string | null {
  const trimmed = result.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return null;
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) {
      return `${parsed.length} item${parsed.length === 1 ? "" : "s"}`;
    }
    if (typeof parsed === "object" && parsed !== null) {
      // Prefer common human-readable fields.
      const preferred = [
        "summary", "message", "content", "result", "output",
        "stdout", "stderr", "error", "status", "data",
        "results", "matches", "items", "files", "count", "total",
      ];
      for (const key of preferred) {
        if (!(key in parsed)) continue;
        const v = parsed[key];
        if (v === undefined || v === null) continue;
        if (typeof v === "string") return compactText(v, 88);
        if (typeof v === "number" || typeof v === "boolean") return String(v);
        if (Array.isArray(v)) {
          const label = key === "results" ? "result" : key === "matches" ? "match" : key === "files" ? "file" : "item";
          return `${v.length} ${label}${v.length === 1 ? "" : "s"}`;
        }
      }
      const keys = Object.keys(parsed);
      if (keys.length > 0) return `${keys.length} field${keys.length === 1 ? "" : "s"}`;
    }
  } catch {
    // Not JSON -- fall through.
  }
  return null;
}

export function firstParam(tc: ToolCallState, keys: string[]): string {
  for (const key of keys) {
    const value = tc.params[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  // The parsed object isn't there yet: `tool_call_end` swaps `params` for the
  // parsed argument object, but every render before it (streaming, and history
  // replay that never re-sends the event) still sees the raw `arguments`
  // string. Read the command out of it so the strip summary is never empty.
  const raw = tc.params?.arguments;
  if (typeof raw === "string" && raw.trim().startsWith("{")) {
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      for (const key of keys) {
        const value = parsed[key];
        if (typeof value === "string" && value.trim()) return value;
      }
    } catch {
      // Partial JSON while the arguments are still streaming — ignore.
    }
  }
  return "";
}

// === Summary helpers ==============================================================
//
// Icon, label, card shape and body are all resolved from the backend-declared
// tool metadata (see `tool_meta.ts`). This file only keeps the logic that turns
// a tool call into short human-readable text.

/**
 * Resolves the page URL a "url"-strategy call targeted (browser family).
 * Prefers the call's own `url` argument (navigate / new_tab); falls back to a
 * LABELED url in the result head ("Navigated to …" / "URL: …"). Free-form
 * result content is never scanned, so a page dump cannot smuggle an arbitrary
 * link into the summary — and the untrusted-result wrapper is never shown.
 */
function toolTargetUrl(tc: ToolCallState): string {
  const direct = firstParam(tc, ["url", "uri"]);
  if (direct) return direct;
  const head = (tc.result || "").slice(0, 400);
  const m = head.match(/(?:\bNavigated to|\bURL:)\s+(\S+)/);
  return m ? m[1] : "";
}

export function getToolSummary(tc: ToolCallState): string {
  const meta = metaFor(tc.name);
  // For tools with result, show result summary instead of params
  if (tc.result) {
    // Backend collapse stub — never leak call ids/previews into the UI.
    // Command/query-shaped tools still lead with their input; everything
    // else renders an empty summary rather than the stub text.
    if (isCollapsedPlaceholder(tc.result)) {
      return meta.body === "terminal" || meta.body === "web-results"
        ? compactText(firstParam(tc, meta.keys), 88)
        : "";
    }
    // Question tool: the collapsed strip shows nothing at all — the Q/A
    // blocks live in the expandable body only.
    if (tc.name === "question") return "";
    // URL-strategy tools (browser): the summary is ONLY the page URL. The raw
    // result is untrusted page content wrapped in delimiters — never dump it.
    if (meta.result === "url") return compactText(toolTargetUrl(tc), 88);
    // Terminal + web-search strips always lead with what the model issued
    // (the command / the query). The stdout or the result list belongs to
    // the expanded body -- on the collapsed line it reads as if the agent
    // had *said* that text, and it pushed the actual intent off the edge.
    if (meta.body === "terminal" || meta.body === "web-results") {
      return compactText(firstParam(tc, meta.keys), 88);
    }
    // If the backend returned JSON, extract a readable field instead of
    // dumping the raw serialized object into the strip summary.
    const jsonSummary = extractJsonSummary(tc.result);
    if (jsonSummary) return jsonSummary;
    if (meta.result === "file") {
      // File tools always show WHICH file was targeted, never the tool result:
      // file_read returns the file's raw content, so falling back to
      // `compactText(tc.result)` turned the summary into a dump of its first
      // lines and hid the file name entirely.
      return compactPath(toolFilePath(tc), 88);
    }
    if (meta.result === "match_count") {
      const match = tc.result.match(/(\d+)\s*match/i);
      return match ? t("chat.toolMatches", { count: parseInt(match[1], 10) }) : compactText(tc.result, 88);
    }
    if (meta.result === "file_count") {
      const count = tc.result.trim().split("\n").filter((l) => l.trim()).length;
      return t("chat.toolFiles", { count });
    }
    if (meta.result === "discover") return t("chat.toolDiscoverTools");
    return compactText(tc.result, 88);
  }
  // No result yet — show minimal param hint (not full params)
  if (meta.body === "terminal") {
    return compactText(firstParam(tc, meta.keys), 96);
  }
  if (meta.result === "file") return compactPath(toolFilePath(tc), 88);
  if (meta.result === "discover") return t("chat.toolDiscoverTools");
  if (meta.keys.length > 0) return compactText(firstParam(tc, meta.keys), 88);
  const keys = Object.keys(tc.params).filter((k) => k !== "id");
  if (keys.length > 0) {
    return compactText(tc.params[keys[0]], 88);
  }
  return "";
}

export function getToolInlineSummary(tc: ToolCallState): string {
  const meta = metaFor(tc.name);
  if (meta.result === "discover") return t("chat.toolDiscoverTools");
  // File tools lead with the target file name — same rule as getToolSummary,
  // just narrower, and never shortened in a way that hides the name.
  if (meta.result === "file") {
    return compactPath(toolFilePath(tc), 42) || getToolSummary(tc);
  }
  // URL-strategy tools (browser): page URL only, never the result dump.
  if (meta.result === "url") return compactText(toolTargetUrl(tc), 42);
  if (meta.keys.length > 0) {
    const hint = firstParam(tc, meta.keys);
    if (hint) return compactText(hint, 42);
    if (meta.hint) return compactText(t(meta.hint), 42);
  }
  const fromParams = firstParam(tc, ["query", "path", "file_path", "url", "name"]);
  return compactText(fromParams || getToolSummary(tc), 42);
}

export function getAgentName(tc: ToolCallState): string {
  // Backend must pass an English agent name via agent_name / name.
  // Fall back to the literal "Agent" (English) so the label never
  // degenerates into a localized string like "智能体".
  const configuredName = (tc.params.agent_name as string) || (tc.params.name as string);
  return (configuredName && configuredName.trim()) || "Agent";
}

/** Normalizes an agent/tool name into a human-friendly display label. */
export function formatAgentLabel(rawName: string): string {
  // The backend supplies the agent name in English; preserve it as-is and
  // only capitalize the first character. Do NOT append "Agent" or lowercase
  // the rest, so names like "Code Reviewer" stay intact.
  const name = (rawName || "").trim();
  if (!name) return "Agent";
  return name.charAt(0).toUpperCase() + name.slice(1);
}

export function parseOption(raw: string): { label: string; desc: string } {
  const trimmed = (raw || "").trim();
  if (!trimmed) return { label: "", desc: "" };
  // Try common separators between title and description.
  const separators = [" / ", " — ", " – ", " - ", ": ", "：", "\n"];
  for (const sep of separators) {
    const idx = trimmed.indexOf(sep);
    if (idx > 0 && idx < trimmed.length - sep.length) {
      return { label: trimmed.slice(0, idx).trim(), desc: trimmed.slice(idx + sep.length).trim() };
    }
  }
  return { label: trimmed, desc: "" };
}

export function getToolBodyText(tc: ToolCallState): string {
  if (!tc.result) return "";
  const meta = metaFor(tc.name);
  const ctx = { tc, result: tc.result, props: meta.props };
  // Terminal tools wrap their output in a JSON envelope; when the result is not
  // JSON the terminal body comes back empty, so show the raw text instead.
  if (meta.body === "terminal") {
    return renderToolBody(ctx, "terminal") || renderToolBody(ctx, "pre");
  }
  return renderToolBody(ctx, meta.body);
}

/**
 * Renders a file-mutation result as a diff. The implementation lives in
 * `renderDiffBody` (./tool_body.js); kept here so existing callers and the
 * module's public surface stay stable.
 */
export function renderDiff(result: string): string {
  return renderDiffBody(result);
}

export function _toolStatusHtml(status: string): string {
  if (status === "running") return `<span class="tool-status-dot running"></span></span>`;
  return "";
}

export function getThinkingSummary(text: string, elapsed?: number): string {
  const summary = compactText(text, 54);
  const duration = elapsed ? `${elapsed}s` : "";
  if (summary && duration) return `${summary} - ${duration}`;
  return summary || duration;
}

/**
 * Re-run Lucide inside `root` ONLY.
 *
 * `root` is deliberately REQUIRED. `lucide.createIcons()` without a root
 * defaults to `document`, and for each `[data-lucide]` node it runs
 * `parentNode.replaceChild(<svg>, <i>)` — it destroys and recreates the node.
 * A document-wide pass therefore churns every icon in the app: the sidebar,
 * the toolbar, an open panel, another view entirely.
 *
 * That churn is what breaks the UI while the model is streaming. A streamed
 * token landing between a mousedown and its mouseup swaps the node under the
 * cursor, so the browser dispatches the resulting `click` on the nearest
 * common ancestor instead of the real target and the interaction is silently
 * swallowed. Open UI also loses its hover/expanded state (it "snaps back"),
 * and the mass replacement forces a synchronous layout that makes scroll
 * anchoring fight the re-render.
 *
 * Always pass the container whose markup was just injected.
 */
export function createLucideIcons(root: HTMLElement): void {
  // Shared boundary: a malformed icon must not abort the surrounding render.
  safeCreateIcons(root);
}

export function saveVideoPlayback(scope?: HTMLElement): Array<{id: string; t: number; p: boolean}> {
  const states: Array<{id: string; t: number; p: boolean}> = [];
  ((scope ?? document) as HTMLElement).querySelectorAll<HTMLVideoElement>(".info-card--media video").forEach(v => {
    const card = v.closest<HTMLElement>(".info-card--media");
    if (card?.id) states.push({ id: card.id, t: v.currentTime, p: v.paused });
  });
  return states;
}

export function restoreVideoPlayback(states: Array<{id: string; t: number; p: boolean}>, scope?: HTMLElement): void {
  if (states.length === 0) return;
  requestAnimationFrame(() => {
    states.forEach(s => {
      // Scoped lookup: the same tc-* id can exist in the main chat and the
      // automation detail; restore into the container being re-rendered, not
      // whichever card the global id resolves to first.
      const card = scope
        ? scope.querySelector<HTMLElement>(`#${CSS.escape(s.id)}`)
        : document.getElementById(s.id);
      if (!card) return;
      const video = card.querySelector<HTMLVideoElement>("video");
      if (!video) return;
      video.currentTime = s.t;
      if (!s.p) {
        const play = () => video.play().catch(() => {});
        if (video.readyState >= 2) play();
        else video.addEventListener("canplay", play, { once: true });
      }
    });
  });
}

/** Flash the copy icon to a checkmark with a fade transition, then revert after 2s. */
export function flashCopyButton(btn: HTMLElement): void {
  const origIcon = btn.getAttribute('data-original-icon');
  const original = origIcon || 'copy';
  if (!origIcon) btn.setAttribute('data-original-icon', original);

  // Keep the actions visible while the checkmark animation plays.
  btn.classList.add('copying', 'copy-flash');

  // The fade itself is CSS (`.copy-flash.is-fading`); these timings only
  // decide *when* the glyph swaps — while the button is fully faded out.
  const fade = TransitionHelper.step('--duration-fast', 120);
  const swapGlyph = (name: string): void => {
    const i = btn.querySelector('[data-lucide]');
    if (i) i.setAttribute('data-lucide', name);
    createLucideIcons(btn);
  };

  btn.classList.add('is-fading');
  setTimeout(() => {
    swapGlyph('check');
    btn.classList.remove('is-fading');
  }, fade);

  setTimeout(() => {
    btn.classList.add('is-fading');
    setTimeout(() => {
      swapGlyph(original);
      btn.classList.remove('is-fading', 'copying', 'copy-flash');
    }, fade);
  }, 2000);
}
