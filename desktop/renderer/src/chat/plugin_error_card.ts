/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
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
 * Unified plugin fault card.
 *
 * When a plugin-sourced tool call ends in error, the chat timeline shows this
 * card instead of the tool's normal card.  Two rules shape it.
 *
 * The voice.  The person reading this has just been interrupted by somebody
 * else's mistake, so nothing here accuses, scolds or alarms — no "failed",
 * no "error", no red.  The card names the plugin, says plainly that their own
 * work is untouched, and then offers the shortest way back to normal.  The
 * technical detail sits underneath for whoever wants it, but it never leads.
 *
 * The shape.  One card, then plain prose.  There is no label/value grid, no
 * statistics row and no separate footer card: the plugin, the tool, the author
 * and the versions share a single quiet line at the bottom, the way a receipt
 * lists its lines.  Only the raw payload gets a block of its own, because
 * arbitrary text needs a monospace box to stay readable.
 *
 * The identity.  The header is the plugin's own name and its own icon — a
 * failing plugin should look exactly like itself everywhere else in the UI,
 * not turn into a generic warning.  A plugin that declares no icon leaves the
 * slot empty; the app does not hand out a glyph it invented for them.
 *
 * The copy is hardcoded English.  An interruption notice is not a place for
 * translation: it is read by someone who is already off-balance, and mixing
 * their UI language into the fault text reads as machine output.  The support
 * channel shown is the first the plugin declares, in this order:
 *
 *   e-mail -> official site -> repository (file an issue) -> docs (self-help)
 *
 * This is the first consumer of a generic fault-display scheme that other
 * extracted surfaces (settings, sessions, automation, ...) reuse when a plugin
 * misbehaves there: each surface renders one fault card from the registered
 * renderer at this slot, so a single component backs every UI.
 */

import type { ToolCallState } from "../core/types.js";
import { BUILTIN_OWNER, ext } from "../core/extensions.js";
import { escapeHtml } from "./chat_markdown.js";
import { PLUGIN_STATES_CHANGED, pluginContact, pluginInfo } from "../core/plugin_info.js";
import { pluginIconHtml } from "../ui/plugin_assets.js";

/** Slot every plugin-fault surface reads from. */
export const PLUGIN_ERROR_SLOT = "chat.card.plugin-error";

/** The fault-card primitives a surface passes down. */
export interface PluginErrorContext {
  /** The failing tool call. */
  tc: ToolCallState;
  /** Plugin identifier (manifest name, tool source, or a free label). */
  plugin: string;
  /** Whether the owning chat already expanded this card. */
  expanded: boolean;
}

export type PluginErrorSpec = {
  id: string;
  owner?: string;
  priority?: number;
  title?: string;
  render: (ctx: PluginErrorContext) => string;
};

// ── App versions ─────────────────────────────────────────────────────────────
//
// `getAppVersions` is an async IPC round-trip, but the card renders inside a
// pure string function.  The value is fetched once, cached, and the chat is
// asked to repaint when it lands; until then the line simply omits it.

let _versions: { desktop?: string; agent?: string } = {};
let _versionsRequested = false;

function requestVersions(): void {
  if (_versionsRequested) return;
  _versionsRequested = true;
  const api = (window as any).electronAPI;
  if (!api?.getAppVersions) return;
  api.getAppVersions()
    .then((v: { desktop?: string; agent?: string }) => {
      _versions = v || {};
      repaint();
    })
    .catch(() => { /* versions are decoration; stay silent */ });
}

/** Ask the chat to rebuild so a card can pick up newly-arrived facts. */
function repaint(): void {
  (window as any).__chatRender?.();
}

// The contact list only exists after the first `plugin_changed` snapshot, and
// the version pair only after the first IPC reply — either may arrive after the
// card was painted, so repaint on the first one.
window.addEventListener(PLUGIN_STATES_CHANGED, repaint);

// ── Copy ─────────────────────────────────────────────────────────────────────

/** The plugin's display name, falling back to the failing tool's own name. */
function faultSubject(ctx: PluginErrorContext, toolName: string): string {
  const declared = pluginInfo(ctx.plugin)?.display_name || ctx.plugin;
  return (declared || toolName || "A plugin").trim();
}

/** `2026-09-30 11:22:33`, locale-independent. */
function formatIncidentTime(epochMs: number | undefined): string {
  if (!epochMs) return "";
  const d = new Date(epochMs);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** How each channel reads in a sentence, so a bare address explains itself. */
const CONTACT_ROLE: Record<string, string> = {
  email: "write to",
  homepage: "see",
  repository: "open an issue at",
  docs: "the self-service notes at",
};

/** The short word that labels a channel on the facts rows. */
const CONTACT_ROW_LABEL: Record<string, string> = {
  email: "e-mail",
  homepage: "site",
  repository: "repo",
  docs: "docs",
};

/**
 * The plugin's own glyph for the strip header, so a failing plugin looks like
 * itself — the same icon the market shows for it, rendered through the one
 * shared plugin-icon layer (`ui/plugin_assets.ts`).
 *
 * A manifest may declare a shipped image path, a lucide icon name or a single
 * emoji.  A plugin that declares none gets an empty slot: the app does not
 * hand out a generic glyph on somebody else's behalf.
 */
function pluginGlyphHtml(name: string | undefined): string {
  return pluginIconHtml(pluginInfo(name)?.icon, name || "", {
    extraClass: "semantic plugin-error-icon",
    emojiClass: "semantic plugin-error-glyph",
  });
}

/**
 * The default renderer shipped as a builtin.  Produces the strip-shaped fault
 * card: a soft header, three sentences of recovery prose, one quiet line of
 * facts, and the raw payload for anyone who needs it.
 */
export function renderPluginErrorCard(ctx: PluginErrorContext): string {
  requestVersions();
  const tc = ctx.tc;
  const id = `tc-${tc.id}`;
  const expanded = ctx.expanded;
  const toolName = tc.name || "plugin";
  const plugin = faultSubject(ctx, toolName);
  const info = pluginInfo(ctx.plugin);
  const contacts = pluginContact(ctx.plugin);
  const preferred = contacts[0];
  const glyphHtml = pluginGlyphHtml(ctx.plugin);
  const payload = typeof tc.result === "string" ? tc.result : "";

  // The backend appends a machine-readable block for the model after the
  // human-readable part; the person reading the card only wants the first part.
  const marker = payload.indexOf("[plugin_fault]");
  const detail = (marker >= 0 ? payload.slice(0, marker) : payload).trim();

  // 1. What happened — named, calm, and explicitly harmless.
  const line1 = `The "${plugin}" plugin stumbled on this one call, so it stopped early. `
    + `Everything you were working on is untouched, so there is nothing to undo here.`;
  // 2. What usually fixes it — the two recoveries the agent can actually run.
  const line2 = `It most often clears up on its own once "${plugin}" is reloaded, `
    + `or you can restart the whole plugin system from Settings whenever it suits you.`;
  // 3. If you would rather not bother — restart the app, or ask the author.
  const line3 = preferred
    ? `If you would rather not wait, restarting Encre usually settles it too. `
      + `And if it keeps coming back, the people behind "${plugin}" will be glad to take a look — `
      + `${CONTACT_ROLE[preferred.kind] || "reach them at"} ${preferred.value}.`
    : `If you would rather not wait, restarting Encre usually settles it too. `
      + `And if it keeps coming back, the people behind "${plugin}" will be glad to take a look.`;

  // The facts as label/value rows, the same rows the model payload lists, so a
  // reader can lift a version or a URL verbatim instead of parsing one run-on
  // line.  A row is dropped when its value is unknown.
  const facts: Array<[string, string]> = [
    ["plugin", info?.version ? `${plugin} v${info.version}` : plugin],
    ["tool", toolName],
  ];
  if (info?.author) facts.push(["author", info.author]);
  for (const extra of contacts.slice(1)) {
    const label = CONTACT_ROW_LABEL[extra.kind];
    facts.push([label || "link", extra.value]);
  }
  if (_versions.desktop) facts.push(["Encre", _versions.desktop]);
  if (_versions.agent) facts.push(["plugin system", _versions.agent]);
  const incident = formatIncidentTime(tc.errorAt);
  if (incident) facts.push(["time", incident]);

  const factRows = facts
    .map(
      ([key, value]) =>
        `<span class="plugin-error-key">${escapeHtml(key)}</span>`
        + `<span class="plugin-error-val">${escapeHtml(value)}</span>`,
    )
    .join("");

  return `<div class="strip-item plugin-error-card${expanded ? " expanded" : ""}" data-id="${id}">
    <div class="strip" onmouseenter="window.__hoverOn(this)" onmouseleave="window.__hoverOff(this)">
      <span class="icon-wrap">
        ${glyphHtml}
        <i data-lucide="chevron-down" class="arrow"></i>
      </span>
      <span class="strip-name">${escapeHtml(plugin)}</span>
    </div>
    <div class="strip-body-slot" data-has-body="1">
      <div class="strip-body plugin-error-body">
        <p class="plugin-error-line">${escapeHtml(line1)}</p>
        <p class="plugin-error-line">${escapeHtml(line2)}</p>
        <p class="plugin-error-line">${escapeHtml(line3)}</p>
        ${factRows ? `<div class="plugin-error-facts">${factRows}</div>` : ""}
        ${detail ? `<pre class="plugin-error-detail">${escapeHtml(detail)}</pre>` : ""}
      </div>
    </div>
  </div>`;
}

// --- slot plumbing, mirroring chat/tool_body.ts ----------------------------

function resolvePluginError(ctx: PluginErrorContext): PluginErrorSpec | null {
  ensurePluginErrors();
  for (const spec of ext.entries<PluginErrorSpec>(PLUGIN_ERROR_SLOT)) {
    return spec;
  }
  return null;
}

/** Render through the registered renderer, never breaking the timeline. */
export function renderPluginError(ctx: PluginErrorContext): string {
  try {
    const spec = resolvePluginError(ctx);
    if (!spec) return "";
    return spec.render(ctx);
  } catch {
    return "";
  }
}

export function registerPluginError(spec: PluginErrorSpec): () => void {
  return ext.register<PluginErrorSpec>(PLUGIN_ERROR_SLOT, spec);
}

export function unregisterPluginError(id: string): boolean {
  return ext.unregister(PLUGIN_ERROR_SLOT, id);
}

export function listPluginErrors(): PluginErrorSpec[] {
  ensurePluginErrors();
  return ext.entries<PluginErrorSpec>(PLUGIN_ERROR_SLOT);
}

let _pluginErrorsRegistered = false;

function ensurePluginErrors(): void {
  if (_pluginErrorsRegistered) return;
  _pluginErrorsRegistered = true;
  ext.register<PluginErrorSpec>(PLUGIN_ERROR_SLOT, {
    id: "builtin-plugin-error",
    owner: BUILTIN_OWNER,
    title: "Plugin fault card",
    render: renderPluginErrorCard,
  });
}
