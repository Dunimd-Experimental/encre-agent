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
 * Tool UI metadata store.
 *
 * The renderer owns no tool names. Every tool ships one declaration from the
 * backend — the `encre.tools.tool_ui` catalog merged with each plugin's
 * `contributes.tools_meta`, plugins winning — over `plugin_ui_sync`, and this
 * module resolves it into the four decisions a tool card needs:
 *
 *   meta.icon     -> the glyph
 *   meta.label    -> the displayed name (terminal tools resolve it per shell)
 *   meta.card     -> which card primitive renders it
 *   meta.body     -> which body primitive renders the result
 *
 * Prefix entries (`memory_`, `task_`, `cron_`, `mcp__`, `codebase`) match by
 * name prefix, longest prefix winning, so a whole tool family is one catalog
 * line instead of N name checks. An unknown tool degrades to a title-cased
 * name and no glyph: the icon that marks a tool belongs to whoever declares
 * it, and the catalog above is this app's own declaration for its core tools.
 *
 * The six file-mutation tools stay hardcoded below, on purpose: they are the
 * core agent's contract with the user, they must render correctly before the
 * first sync arrives, and no plugin may restyle them.
 */

import type {
  PluginToolUiBody,
  PluginToolUiCard,
  PluginToolUiMeta,
  PluginToolUiResult,
} from "../core/types.js";
import { pluginIconHtml } from "../ui/plugin_assets.js";

// The wire shapes live in `core/types.js` next to every other server message;
// re-exported here so chat-layer callers keep a single import point.
export type { PluginToolUiBody, PluginToolUiCard, PluginToolUiMeta, PluginToolUiResult };

/** One tool's UI declaration, as received from the backend. */
export type ToolUiMeta = PluginToolUiMeta;

/** One wire entry `{ name, meta, source }`, typed leniently while parsing. */
export type ToolUiEntry = {
  name?: string;
  meta?: PluginToolUiMeta;
  source?: string;
};

// === Core tools the renderer must never lose ================================

/** Shared declaration for the write/edit tools: strip card, diff body. */
const FILE_UI: Pick<PluginToolUiMeta, "icon" | "card" | "body" | "result"> = {
  icon: "pencil-line",
  card: "strip",
  body: "diff",
  result: "file",
};

/**
 * The six file-mutation tools, plus their provider aliases.
 *
 * `apply_patch` is one of the six but deliberately keeps the item card and a
 * plain body: it is not treated as a direct file write/edit, so it never gets
 * the diff strip. `delete_file` is the only one that renders as a file card.
 */
export const HARD_CODED_TOOL_UI: Record<string, ToolUiMeta> = {
  // Interactive ask-the-user tool. `card: "question"` routes every live state
  // to the dedicated question card; answered asks re-render through
  // renderQuestionDoneCard (same card shell, folded like the thinking strip).
  question: { icon: "circle-help", label: "Question", card: "question", body: "pre", result: "text" },
  write: { ...FILE_UI, label: "Write File" },
  file_write: { ...FILE_UI, label: "Write File" },
  write_file: { ...FILE_UI, label: "Write File" },
  edit: { ...FILE_UI, label: "Edit File" },
  file_edit: { ...FILE_UI, label: "Edit File" },
  edit_file: { ...FILE_UI, label: "Edit File" },
  apply_patch: { icon: "git-merge", label: "Apply Patch", card: "item", body: "pre", result: "file" },
  delete_file: { icon: "trash-2", label: "Delete File", card: "file", body: "pre", result: "file" },
};

/**
 * A fully-resolved declaration: every field the renderer reads is present, so
 * callers never need a `|| []` guard.
 */
export type ResolvedToolMeta = {
  icon?: string;
  label?: string;
  card: PluginToolUiCard;
  body: PluginToolUiBody;
  hidden: boolean;
  prefix: boolean;
  result: PluginToolUiResult;
  keys: string[];
  hint?: string;
  terminal_labels?: Record<string, string>;
  /** Strip-header actions suppressed by the declaration (e.g. "stop"). */
  hide: string[];
  /** Free-form scalars forwarded to the body renderer. */
  props?: Record<string, string | number | boolean>;
};

let _table: Map<string, ToolUiMeta> = new Map();
let _sources: Map<string, string> = new Map();
let _cache: Map<string, ResolvedToolMeta> = new Map();
/** Signature of the last accepted table; detects no-op syncs. */
let _tableSig = "";

/** Normalizes an entry so callers never hit an undefined field. */
function normalize(meta: ToolUiMeta | undefined): ResolvedToolMeta {
  return {
    icon: meta?.icon,
    label: meta?.label,
    card: meta?.card || "strip",
    body: meta?.body || "pre",
    hidden: !!meta?.hidden,
    prefix: !!meta?.prefix,
    result: meta?.result || "text",
    keys: meta?.keys && meta.keys.length ? meta.keys : [],
    hint: meta?.hint,
    terminal_labels: meta?.terminal_labels,
    hide: meta?.hide && meta.hide.length ? meta.hide : [],
    props: meta?.props,
  };
}

/**
 * Shallow-merges an override onto a resolved base, for `extends` chains:
 * explicitly-set fields win, `props` merge key-wise, everything else replaces.
 */
function mergeResolved(base: ResolvedToolMeta, override: ToolUiMeta): ResolvedToolMeta {
  const top = normalize(override);
  return {
    icon: top.icon ?? base.icon,
    label: top.label ?? base.label,
    card: override.card ? top.card : base.card,
    body: override.body ? top.body : base.body,
    hidden: override.hidden !== undefined ? top.hidden : base.hidden,
    prefix: override.prefix !== undefined ? top.prefix : base.prefix,
    result: override.result ? top.result : base.result,
    keys: override.keys && override.keys.length ? top.keys : base.keys,
    hint: top.hint ?? base.hint,
    terminal_labels: override.terminal_labels ? top.terminal_labels : base.terminal_labels,
    hide: override.hide && override.hide.length ? top.hide : base.hide,
    props: base.props || top.props ? { ...base.props, ...top.props } : undefined,
  };
}

function titleCase(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

/**
 * Matches provider aliases of the core write/edit/delete tools, e.g.
 * `file_write_v2`. These are part of the hardcoded six, not catalog data.
 */
function isFileMutationAlias(name: string): boolean {
  return /(write|edit|delete)/i.test(name) && /file/i.test(name);
}

/**
 * Last resort for a name nobody declared. An alias of one of the six core
 * file-mutation tools keeps the diff strip so it still reads as a file change;
 * anything else gets a neutral strip card, so an unknown tool never renders blank.
 */
function aliasOrNeutral(name: string): ResolvedToolMeta {
  if (isFileMutationAlias(name)) return normalize({ ...FILE_UI, label: titleCase(name) });
  return normalize(undefined);
}

/**
 * Replaces the whole table. Called once per `plugin_ui_sync`; idempotent and
 * safe with an empty list, which simply falls back to the hardcoded core and
 * the neutral glyph.
 *
 * Returns true only when the catalog actually changed. A client connecting
 * during backend plugin warmup receives an empty sync first and the real
 * catalog later; the caller announces real changes so already-rendered tool
 * cards restyle immediately instead of keeping their neutral fallback look
 * until an unrelated view switch forces a rebuild.
 */
export function setToolMetaEntries(entries: ToolUiEntry[]): boolean {
  const next = new Map<string, ToolUiMeta>();
  const sources = new Map<string, string>();
  for (const entry of entries) {
    if (!entry || typeof entry.name !== "string" || !entry.name.trim()) continue;
    next.set(entry.name.trim(), entry.meta || {});
    if (typeof entry.source === "string" && entry.source) sources.set(entry.name.trim(), entry.source);
  }
  // Order-independent signature: reconnects may reorder the catalog.
  const sig = [...next.keys()].sort()
    .map((k) => `${k}\u0000${JSON.stringify(next.get(k))}\u0000${sources.get(k) ?? ""}`)
    .join("\u0001");
  if (sig === _tableSig) return false;
  _tableSig = sig;
  _table = next;
  _sources = sources;
  _cache = new Map();
  return true;
}

export function toolMetaCount(): number {
  return _table.size;
}

/**
 * The origin of a tool entry, as declared by the backend: `builtin-catalog` /
 * `builtin-tool` for core tools, an arbitrary plugin id otherwise. Missing when
 * the entry had no source (including the hardcoded six).
 */
export function toolSource(name: string): string | undefined {
  const cached = _sources.get(name);
  if (cached) return cached;
  for (const [key, meta] of _table) {
    if (meta && typeof (meta as { prefix?: boolean }).prefix === "boolean" && (meta as { prefix?: boolean }).prefix && /^[A-Za-z0-9_]+$/.test(key) && name.startsWith(key)) {
      const src = _sources.get(key);
      if (src) return src;
    }
  }
  return undefined;
}

/**
 * True when a tool is contributed by a plugin (or the MCP bridge) rather than
 * the core loop. Frontend renders the unified plugin fault card for plugin
 * tool calls that end in error; core tools always keep their own card.
 */
export function isPluginTool(name: string): boolean {
  if (name.startsWith("mcp__") || name.startsWith("plugin__")) return true;
  const source = toolSource(name);
  if (!source) return false;
  return source !== "builtin-catalog" && source !== "builtin-tool";
}

/**
 * Depth cap for `extends` chains. A base may itself extend another declared
 * tool; the cap also breaks reference cycles (A -> B -> A).
 */
const MAX_EXTENDS_DEPTH = 4;

/**
 * Resolves `extends` chains: the named base (any table entry or hardcoded
 * declaration) is resolved first, then this meta's own fields are merged on
 * top. Lets a plugin declare a terminal-shaped tool with just an icon and
 * `extends: "terminal"` instead of restating the whole card shape.
 */
function resolveExtends(meta: ToolUiMeta | undefined, depth: number): ResolvedToolMeta {
  const own = normalize(meta);
  if (!meta || depth >= MAX_EXTENDS_DEPTH) return own;
  const baseName = meta.extends;
  if (!baseName) return own;
  const baseMeta = _table.get(baseName) || HARD_CODED_TOOL_UI[baseName];
  if (!baseMeta) return own;
  const base = resolveExtends(baseMeta, depth + 1);
  return mergeResolved(base, meta);
}

/**
 * Resolves the UI declaration for a tool name.
 *
 * Order: hardcoded core (unoverridable) -> exact catalog entry -> longest
 * prefix family -> file-mutation alias -> neutral fallback. Exact and prefix
 * entries additionally resolve their `extends` chain. The result is cached per
 * name until the next sync, so repeated renders of the same call are a Map
 * lookup.
 */
export function metaFor(name: string): ResolvedToolMeta {
  const cached = _cache.get(name);
  if (cached) return cached;
  const hardcoded = HARD_CODED_TOOL_UI[name];
  let resolved: ResolvedToolMeta;
  if (hardcoded) {
    // Hardcoded core is checked before the table, so no sync can restyle it.
    resolved = normalize(hardcoded);
  } else {
    let raw: ToolUiMeta | undefined;
    if (_table.has(name)) {
      raw = _table.get(name);
    } else {
      let bestLen = 0;
      for (const [key, meta] of _table) {
        if (!meta.prefix || !name.startsWith(key) || key.length <= bestLen) continue;
        raw = meta;
        bestLen = key.length;
      }
    }
    resolved = raw ? resolveExtends(raw, 0) : aliasOrNeutral(name);
  }
  _cache.set(name, resolved);
  return resolved;
}

/** True for tools that render nothing in the timeline. */
export function isToolHidden(name: string): boolean {
  const meta = metaFor(name);
  return meta.hidden || meta.card === "hidden";
}

/**
 * The tool glyph its owner declared, or the empty string.
 *
 * Callers skip the icon element when nothing comes back: the glyph that marks
 * a tool is the declaring plugin's to choose, not ours to hand out.
 */
export function iconFor(name: string): string {
  return metaFor(name).icon || "";
}

/**
 * The tool glyph as markup, honouring every shape an icon declaration may take.
 *
 * A declaring plugin picks its glyph from the same three options the market
 * uses, so the chat card renders it through the same helper: a package-relative
 * asset path (`assets/icon.svg`), a lucide name, or an emoji. Asset paths in
 * the core catalog carry no owning plugin, so they are written fully qualified
 * as `<plugin id>/<relpath>`; a path declared by a plugin resolves against that
 * plugin's own package directory.
 *
 * Returns the empty string when nothing was declared, which callers use to skip
 * the icon element entirely.
 */
export function toolIconHtml(name: string, extraClass?: string): string {
  const icon = iconFor(name);
  if (!icon) return "";
  const slash = icon.indexOf("/");
  if (slash > 0) {
    return pluginIconHtml(icon.slice(slash + 1), icon.slice(0, slash), { extraClass });
  }
  return pluginIconHtml(icon, toolSource(name) || "", { extraClass });
}

/**
 * The displayed tool name. Terminal-shaped tools resolve it from the `terminal`
 * argument when their declaration carries `terminal_labels`; everyone else gets
 * the declared label, or a title-cased name as the last resort.
 */
export function labelFor(name: string, terminal?: string): string {
  const meta = metaFor(name);
  if (terminal && meta.terminal_labels) {
    return meta.terminal_labels[terminal] || titleCase(terminal);
  }
  if (meta.label) return meta.label;
  if (name.startsWith("mcp__")) return "MCP";
  if (name === "agent") return "Agent";
  return titleCase(name);
}
