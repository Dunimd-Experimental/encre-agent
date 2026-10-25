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
 * Plugin market view — web app-store style.
 *
 * A content-area sub-view sharing the #agents-view slot and motion contract
 * (see the "Sub-view switch" block in styles.css).  Two data planes merge
 * into one browsable store:
 *
 *   - the LOCAL registry snapshot: core/stream.ts re-dispatches every
 *     `plugin_changed` frame as `encre-plugin-states` carrying
 *     PluginStateEntry[] (name, version, state, installed, enabled, ...);
 *   - the CENTRAL index (ea-cwh): `market_catalog` frames arrive on connect
 *     and whenever the daily index refresh changes, re-dispatched as
 *     `encre-market-catalog` with curated display fields, the pinned
 *     version and the PyPI package per published plugin.
 *
 * Cards/detail render the merge (MarketItem): local state wins for what is
 * installed and drives the "可更新" badge when the index pins a newer
 * version than the local one.  "Installed" means the package is present on
 * this machine — the app's own system-default packages and entry-point
 * installs included — not merely written in `installed.json`, which only
 * market/zip installs ever touched.
 *
 * Both planes are mirrored into `core/plugin_info.ts` and
 * `core/market_index.ts` by the stream, and `hydrate()` reads them back on
 * open: the frames go out during boot while this view can only be opened by
 * a click afterwards, so listening alone would wait for a push that already
 * happened.
 *
 * Layout: two tabs (发现 / 已安装) sharing the sub-view tab control with
 * Automation, with the search box docked right — one flat store page.  The
 * 发现 tab is the store front of the CENTRAL index ONLY: a plugin that
 * never went through ``encre-plugin publish`` has no curated entry and is
 * not browsable there; locally installed plugins surface in 已安装.  The
 * grid is paged (IntersectionObserver sentinel) and the search is debounced
 * so hundreds of entries stay smooth.
 *
 * An index that has published nothing is reported as such — its real version
 * and entry count, never a shelf of made-up entries — and the view then
 * opens on 已安装, because that is the half of the store this machine can
 * actually show.
 *
 * Real protocol wiring:
 *   install/update -> {type:"market_install", name} (ledger + activation;
 *                     the backend pulls the pinned wheel when the index
 *                     is newer)
 *   uninstall -> {type:"plugin_uninstall", name}   (asked first, Dialog.confirm)
 *   enable    -> {type:"plugin_enable", name}
 *   disable   -> {type:"plugin_disable", name}     (asked first, Dialog.confirm)
 *   skill text -> {type:"get_skill_detail", name}  -> `skill_detail` (body + the
 *                     package's own file list)
 *   skill file -> {type:"get_skill_file", name, path} -> `skill_file`, read from
 *                     inside the skill's own directory
 * Results arrive back as `encre-plugin-lifecycle-ok` / `-error` DOM events
 * plus an authoritative `encre-plugin-states` snapshot, which re-renders.
 *
 * `mandatory` tier entries are runtime core and hidden from the market.
 */

import { t } from "./i18n.js";
import { send } from "../core/ws.js";
import { getState, openRoutedLink, showToast } from "../core/state.js";
import { PERMISSION_MODES, permissionModeGroup, permissionModeLabel, permissionModeIcon, guardPermissionMode, dialogPermissionConfirm } from "../core/permission_mode.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { guardHtml, safeCreateIcons } from "../core/ui_boundary.js";
import { renderDropdownImpl, bindDropdownImpl, type DropdownOption } from "../ui/dropdown.js";
import { runScheduledRenderNow } from "../core/render_scheduler.js";
import { registerSurface, unregisterSurface } from "../core/scope.js";
import { hideViewAnimated, removeAnimated, showViewAnimated } from "../ui/animate.js";
import { renderCrumbs } from "../ui/crumbs.js";
import { showAnchoredDropdown } from "../ui/context-menu.js";
import { Dialog } from "../ui/dialog.js";
import { PROGRESS_HOLD_MS, PROGRESS_WATCHDOG_MS, progressBar } from "../ui/progress.js";
import { metaFor, toolIconHtml } from "../chat/tool_meta.js";
import { pluginIconHtml } from "../ui/plugin_assets.js";
import { platformIconHtml } from "../ui/icons.js";
import { handleMarkdownContentClick, renderMarkdown } from "../chat/chat_markdown.js";
import { pluginStates } from "../core/plugin_info.js";
import { marketIndex } from "../core/market_index.js";
import type { MarketCatalogEntry, PluginStateEntry } from "../core/types.js";

/** In-flight lifecycle operation per plugin name; cleared by the next
 *  snapshot / lifecycle reply.  Renders spinner-disabled buttons so a click
 *  cannot double-fire while the server works. */
type PendingOp = "install" | "uninstall" | "enable" | "disable";

/** Collapse `references/../notes.md` into `notes.md` the way a link inside a
 *  skill document spells it, so the reader can match it against the list of
 *  files the package really carries. */
function resolveSkillPath(path: string): string {
  const out: string[] = [];
  for (const part of path.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") out.pop();
    else out.push(part);
  }
  return out.join("/");
}

/** One market card: the merge of the local registry state and the central
 *  (ea-cwh) catalogue entry under the same plugin name.  Either plane can
 *  be absent — a bundled plugin unknown to the index, or a catalogue-only
 *  plugin that has never been installed here. */
interface MarketItem {
  name: string;
  /** Display version: the local one when installed, else the index one. */
  version: string;
  description: string;
  /** Short human-friendly label; blank falls back to `name` when rendered. */
  displayName: string;
  author: string;
  icon: string;
  tags: string[];
  permissions: string[];
  /** Engine-level resource grants (the fail-closed layer). */
  resources: Record<string, unknown>;
  /** Renderer surfaces the plugin may reach. */
  uiPermissions: Record<string, unknown>;
  provides: Record<string, string[]>;
  homepage: string;
  repository: string;
  docs: string;
  email: string;
  local?: PluginStateEntry;
  central?: MarketCatalogEntry;
  installed: boolean;
  /** Index pins a strictly newer version than the installed one. */
  updatable: boolean;
}

/**
 * A manifest states a machine grant (``filesystem:write``); a person needs to
 * know what that means and how much it costs.  Each known grant maps to one
 * sentence-shaped label, the glyph of the capability, and a risk tier; the
 * raw id stays under the label so an auditor still sees the exact grant.
 * Unknown grants are shown as-is rather than guessed at.
 */
interface PermMeta {
  icon: string;
  labelKey: string;
  risk: "low" | "mid" | "high";
}

const PERM_META: Record<string, PermMeta> = {
  "filesystem:read": { icon: "file-text", labelKey: "market.permRead", risk: "low" },
  "filesystem:write": { icon: "file-pen", labelKey: "market.permWrite", risk: "mid" },
  network: { icon: "globe", labelKey: "market.permNetwork", risk: "mid" },
  "network:outbound": { icon: "globe", labelKey: "market.permNetwork", risk: "mid" },
  "process:exec": { icon: "terminal", labelKey: "market.permExec", risk: "high" },
  shell: { icon: "terminal", labelKey: "market.permExec", risk: "high" },
};

function permMeta(id: string): PermMeta {
  const key = (id || "").trim().toLowerCase();
  const exact = PERM_META[key];
  if (exact) return exact;
  if (key.startsWith("filesystem:write")) return PERM_META["filesystem:write"];
  if (key.startsWith("filesystem")) return PERM_META["filesystem:read"];
  if (key.startsWith("network") || key.includes("http")) return PERM_META.network;
  if (key.startsWith("process") || key.startsWith("shell")) return PERM_META["process:exec"];
  return { icon: "shield", labelKey: "market.permOther", risk: "mid" };
}

/**
 * The two deeper grant layers use the engine's own key names as identifiers
 * (``file_write``, ``process_spawn``, ``tool_meta``), so they are NOT
 * translated — a translated identifier cannot be matched against a manifest.
 * Only their risk tier and the allowance wording are copy: writing files,
 * spawning processes, writing memory and config are the grants that can
 * actually cost the user something, so they sit in the high tier.
 */
const GRANT_ICONS: Record<string, string> = {
  network: "globe",
  network_listen: "radio",
  file_read: "file-text",
  file_write: "file-pen",
  process_spawn: "terminal",
  environment: "variable",
  clipboard: "clipboard",
  screen: "monitor",
  memory_read: "brain",
  memory_write: "brain",
  session_read: "messages-square",
  llm_call: "sparkles",
  config_read: "settings",
  config_write: "settings",
  system_info: "cpu",
  all: "shield",
  providers: "layout-panel-left",
  tool_meta: "wrench",
  contributions: "blocks",
};

const GRANT_HIGH = new Set([
  "file_write", "process_spawn", "memory_write", "config_write", "all",
]);
const GRANT_LOW = new Set([
  "file_read", "memory_read", "system_info", "providers", "tool_meta", "contributions",
]);

function grantMeta(key: string): { icon: string; risk: "low" | "mid" | "high" } {
  const id = (key || "").trim().toLowerCase();
  const risk = GRANT_HIGH.has(id) ? "high" : GRANT_LOW.has(id) ? "low" : "mid";
  return { icon: GRANT_ICONS[id] || "shield", risk };
}

/** A grant's value as one readable line: wildcard lists join, booleans read
 *  as 已允许 / 已拒绝, and anything else is shown as its literal form. */
function grantValueText(value: unknown): string {
  if (value === true) return t("market.permAllowed");
  if (value === false) return t("market.permDenied");
  if (Array.isArray(value)) return value.map((v) => String(v)).join(" · ");
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

/** One shelf of the store: what kind of contribution a plugin makes. */
type MarketGroup = "skill" | "mcp" | "agent" | "tool" | "core";

/**
 * A package whose whole identity IS its tools (``ea-tool-*``, or anything
 * that contributes nothing but tools).  Its detail page does not list them:
 * printing "工具: grep" under a page titled "ea-tool-grep" repeats what the
 * page already says.  Every other kind — 技能 / 连接器 / 智能体 / 核心插件 —
 * does use tools, so there the section is exactly what the reader needs.
 */
function isToolOnlyPackage(a: MarketItem): boolean {
  // The shelf IS the identity: something filed under 工具 has nothing to gain
  // from a list that repeats its own tools.  Skills, connectors, agents and
  // core plugins sit on other shelves and keep the section.
  return groupOf(a) === "tool";
}

/** Tags every package carries (packaging metadata, not a shelf topic). */
const GENERIC_TAGS = new Set(["encre", "ea", "official", "plugin", "plugins", "system", "system-default"]);

/** The word the hero sets after 发现 — always English, like the brand word
 *  itself, in every locale, and each shelf gets its own colour so the
 *  headline says which store you are in before you read the word. */
const HERO_BRAND: Record<MarketGroup | "all", { label: string; color: string }> = {
  all: { label: "Plugins", color: "#2563eb" },
  skill: { label: "Skills", color: "#059669" },
  mcp: { label: "Connectors", color: "#0891b2" },
  agent: { label: "Agents", color: "#7c3aed" },
  tool: { label: "Tools", color: "#ea580c" },
  core: { label: "Core", color: "#475569" },
};

/** Five fixed residents per shelf: bundled plugins whose icons stand for
 *  that store face. Shelves without bundled residents (connectors, agents,
 *  core) show their own items instead — the banner's icons must belong to
 *  the shelf you are standing in, never one shared set for every tab. */
const HERO_RESIDENTS: Partial<Record<MarketGroup | "all", string[]>> = {
  all: ["ea-tool-agent", "ea-tool-browser", "ea-tool-image", "ea-tool-database", "ea-tool-email"],
  skill: ["ea-skill-deep-research", "ea-skill-data-analysis", "ea-skill-audio", "ea-skill-image-generation", "ea-skill-canvas-design"],
  tool: ["ea-tool-agent", "ea-tool-browser", "ea-tool-image", "ea-tool-database", "ea-tool-email"],
};

/**
 * The shelf a plugin belongs on.  A plugin can declare several surfaces
 * (a tool plugin also ships its use-instructions as a skill), so the order
 * decides: an MCP server or an agent provider is shelved as such, a plugin
 * that exposes tools is a 工具, everything else that ships skills is a 技能,
 * and the mandatory tier is 核心插件.  Name prefixes are the last resort for
 * index-only entries whose declaration the index has not published.
 */
function groupOf(a: MarketItem): MarketGroup {
  if (a.local?.tier === "mandatory") return "core";
  const p = a.provides || {};
  if ((p.mcp || []).length || (p.gateways || []).length) return "mcp";
  if ((p.agents || []).length) return "agent";
  if ((p.tools || []).length) return "tool";
  if ((p.skills || []).length) return "skill";
  if (a.name.startsWith("ea-tool-")) return "tool";
  if (a.name.startsWith("ea-skill-")) return "skill";
  return "core";
}

/** Card icon for one market item.  Gateway connector plugins (S8:
 *  provides.gateways) show the same platform icon file the connector
 *  settings panel uses (keyed by adapter id, e.g. "qqbot"), never the
 *  manifest's generic lucide fallback — the store face and the panel face
 *  must be the same glyph.  Everything else keeps its manifest icon. */
function itemIconHtml(a: MarketItem): string {
  const gateways = (a.provides || {}).gateways || [];
  if (gateways.length) return platformIconHtml(gateways[0], 22);
  return pluginIconHtml(a.icon, a.name, { emojiClass: "market-emoji" });
}

/** Dotted-version compare (>0 when `a` is newer).  Numeric segments
 *  compare numerically, others lexically; missing trailing segments count
 *  as "0".  Heuristic, not full PEP440 — mirrors the backend's
 *  `market_catalog.version_gt`. */
function vcmp(a: string, b: string): number {
  const pa = String(a || "").split(/[._+-]/);
  const pb = String(b || "").split(/[._+-]/);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const sa = i < pa.length ? pa[i] : "0";
    const sb = i < pb.length ? pb[i] : "0";
    if (sa === sb) continue;
    const na = Number(sa);
    const nb = Number(sb);
    if (Number.isInteger(na) && Number.isInteger(nb)) return na > nb ? 1 : -1;
    return sa > sb ? 1 : -1;
  }
  return 0;
}

function esc(text: string): string {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Which catalogue an instance speaks for.  The two surfaces share this
 *  whole view — same top bar, same hero, same chips, same rows, same detail
 *  page — and differ only in the data plane behind them. */
export type MarketMode = "remote" | "installed";

export interface MarketOptions {
  /** "remote" (default) = the central index; "installed" = this machine. */
  mode?: MarketMode;
  /** Embedded inside settings: no view animation, no 已安装/添加插件 header
   *  actions (they name surfaces that only make sense in the app view). */
  embedded?: boolean;
}

export class Market {
  private el: HTMLElement;
  private mode: MarketMode;
  private embedded: boolean;
  private body: HTMLElement | null = null;
  /** The category tab strip in the top bar (全部 / 技能 / 连接器 / …). */
  private cats: HTMLElement | null = null;
  private tabbar: HTMLElement | null = null;
  /** The crumb band the detail page lives under (shared crumbs component). */
  private crumbBar: HTMLElement | null = null;
  /** Its right end: the ⋮ menu holding 停用 / 卸载 for an installed plugin. */
  private pageTools: HTMLElement | null = null;
  /** The ⋮ page menu and its click-away backdrop (set while open). */
  private moreMenu: HTMLElement | null = null;
  private moreBackdrop: HTMLElement | null = null;
  private built = false;
  private bound = false;
  private isOpen = false;
  private mounted = false;

  private query = "";
  /** The active category tab — "all" plus the five contribution shelves. */
  private category: MarketGroup | "all" = "all";
  /** The active tag chip (a plugin-declared tag), "" for 全部. */
  private tagFilter = "";
  private detailName: string | null = null;

  /** The live registry snapshot — the market's local data plane. */
  private states: PluginStateEntry[] = [];
  /** Central index (ea-cwh) entries — the market's catalogue data plane. */
  private central: MarketCatalogEntry[] = [];
  private centralSource = "none";
  /** Curated-index provenance, shown verbatim in the empty state so an
   *  empty store reads as "this index has published nothing yet" instead of
   *  looking like a failed load. */
  private indexVersion = "";
  private generatedAt = "";
  /** True once each plane delivered its first snapshot (even an empty
   *  one).  The "loading" state only applies while BOTH are outstanding —
   *  after that an empty market is a legitimate state, not a wait. */
  private statesArrived = false;
  private catalogArrived = false;
  private pending = new Map<string, PendingOp>();
  /** When each in-flight operation's progress bar started, so the bar can hold
   *  for one full sweep even when the reply lands sooner. */
  private pendingSince = new Map<string, number>();
  /** A repaint asked for while an operation was in flight.  It is done the
   *  moment the bar retires, never in the middle of it. */
  private repaintOwed = false;
  /** Skill texts the backend answered, by skill name. */
  private skillDetails = new Map<string, { description: string; body: string; files: string[] }>();
  /** Files of a skill package already read, keyed `skill::path`. */
  private skillFiles = new Map<string, string>();
  /** Which of those reads came back unreadable, so the reader says so once. */
  private skillFileErrors = new Set<string>();
  /** Reads already asked for, so a redraw cannot double-fire one request. */
  private skillFilePending = new Set<string>();
  /** The floating SKILL.md reader, while it is open. */
  private skillReader: HTMLElement | null = null;
  /** Which skill the reader shows and the files walked into, oldest first. */
  private readerView: { skill: string; trail: string[] } | null = null;
  /** The 添加插件 editor card (`.panel-editor` on #main-content), while open. */
  private addEditor: HTMLElement | null = null;

  /** The current filtered result — the grouped list drawn on screen. */
  private list: MarketItem[] = [];
  private searchTimer: number | null = null;

  constructor(root?: HTMLElement | null, opts: MarketOptions = {}) {
    this.el = root || document.getElementById("market-view")!;
    this.mode = opts.mode || "remote";
    this.embedded = !!opts.embedded;
    this.el.addEventListener("click", (e) => this.onClick(e));
    this.el.addEventListener("input", (e) => this.onInput(e));
  }

  /**
   * Paint into an embedded host (settings → 插件 panel).  Same shell, same
   * rendering path as the app view; only the view animation is skipped,
   * because the host panel owns its own visibility.
   */
  mount(): void {
    if (!this.mounted) {
      this.mounted = true;
      this.buildShell();
      this.bindEvents();
    }
    this.isOpen = true;
    this.hydrate();
    this.renderBody();
  }

  open(): void {
    if (this.isOpen) return;
    this.isOpen = true;
    if (!this.built) this.buildShell();
    if (!this.bound) this.bindEvents();
    this.hydrate();
    this.renderBody();
    showViewAnimated(this.el);
  }

  close(): void {
    if (!this.isOpen) return;
    this.isOpen = false;
    this.closeMoreMenu();
    this.skillReader?.remove();
    this.skillReader = null;
    this.closeAddPanel();
    this.pending.clear();
    this.pendingSince.clear();
    hideViewAnimated(this.el);
  }

  toggle(): void {
    if (this.isOpen) this.close();
    else this.open();
  }

  get active(): boolean {
    return this.isOpen;
  }

  // ── Add-plugin editor (`.panel-editor` on #main-content) ───────────

  /**
   * Open the 添加插件 editor.  One form, entered directly (the back arrow is
   * visible from the start and dismisses the editor): the first two rows pick
   * the plugin type (agent / MCP / skill package) and the install method
   * (manual configuration / ZIP package — the skill package is ZIP-only), and
   * the body below swaps to the matching form.  Both paths funnel into the
   * standard-plugin install pipeline: a manual config is turned into a
   * manifest package by the backend, a ZIP goes through plugin_install.
   */
  private openAddPanel(): void {
    if (this.addEditor?.isConnected) return;
    const host = document.getElementById("main-content");
    if (!host) return;

    const editor = document.createElement("div");
    editor.className = "panel-editor";
    editor.dataset.marketAdd = "1";

    const envRowHtml = (): string => `
      <div class="mcp-env-row">
        <input type="text" class="model-form-input mcp-env-key" placeholder="${t("settings.mcpEnvKey")}" />
        <input type="text" class="model-form-input mcp-env-value" placeholder="${t("settings.mcpEnvValue")}" />
        <button type="button" class="btn-icon mcp-env-remove" data-tooltip="${t("settings.remove")}">
          <i data-lucide="trash-2" class="lucide"></i>
        </button>
      </div>`;

    // Five plugin kinds the installer covers; each maps to exactly one
    // install pipeline (see refreshBody / submit) so a kind can never leak
    // into another's path.
    const typeOptions: DropdownOption[] = [
      { id: "agent", label: t("market.addTypeAgent") },
      { id: "mcp", label: t("market.addTypeMcp") },
      { id: "skill", label: t("market.addTypeSkill") },
      { id: "tool", label: t("market.addTypeTool") },
      { id: "core", label: t("market.addTypeCore") },
    ];
    const methodOptionsFull: DropdownOption[] = [
      { id: "manual", label: t("market.addMethodManual") },
      { id: "zip", label: t("market.addMethodZip") },
      { id: "plugin", label: t("market.addMethodPlugin") },
    ];
    const transportOptions: DropdownOption[] = [
      { id: "stdio", label: t("settings.transportTagStdio") },
      { id: "http", label: t("settings.transportTagHttp") },
    ];
    // Execution-model options for the agent form — enabled models only (the
    // same filter the automation dialog applies), ids are flat modelConfigs
    // indexes.  The permission picker mirrors the four chat permission modes.
    const allModels = getState().modelConfigs || [];
    const enabledModels = allModels.filter((m) => m.enabled !== false);
    const modelOptions: DropdownOption[] = enabledModels.map((m) => ({
      id: String(allModels.indexOf(m)),
      label: m.name,
    }));
    // The initially selected execution model falls back to the first enabled
    // one when the active index points at a disabled model.
    let agentModelIndex = (() => {
      const idx = getState().activeModelIndex;
      if (allModels[idx] && allModels[idx].enabled !== false) return idx;
      const first = allModels.findIndex((m) => m.enabled !== false);
      return first >= 0 ? first : idx;
    })();
    let agentPerm = permissionModeGroup("default");

    editor.innerHTML = `
      <div class="toast-dialog panel-editor-dialog encre-dialog-card">
        <div class="toast-title panel-editor-title">
          <button type="button" class="btn-icon btn-icon--sm panel-editor-back" data-tooltip="${t("sidebar.back")}">
            <i data-lucide="arrow-left" class="lucide"></i>
          </button>
          <span class="panel-editor-title-text">${t("market.addPlugin")}</span>
        </div>
        <div class="dialog-body">
          <div class="model-form-row auto-model-perm-row">
            <div class="auto-model-perm-col">
              <label class="model-form-label">${t("market.addTypeLabel")}</label>
              <div class="model-form-dropdown-row">
                ${renderDropdownImpl("market-add-type-dd", typeOptions, "agent", () => {})}
              </div>
            </div>
            <div class="auto-model-perm-col">
              <label class="model-form-label">${t("market.addMethodLabel")}</label>
              <div class="model-form-dropdown-row" data-add="method-dd-slot">
                ${renderDropdownImpl("market-add-method-dd", methodOptionsFull, "manual", () => {})}
              </div>
            </div>
          </div>
          <div class="market-add-form hidden" data-add-body="agent">
            <div class="model-form-row">
              <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.subAgentName")}</label>
              <input type="text" class="model-form-input" data-add="agent-name" placeholder="${t("settings.subAgentNamePlaceholder")}" />
            </div>
            <div class="model-form-row">
              <label class="model-form-label">${t("settings.description")}</label>
              <input type="text" class="model-form-input" data-add="agent-desc" placeholder="${t("settings.subAgentDescPlaceholder")}" />
            </div>
            <div class="model-form-row auto-model-perm-row">
              <div class="auto-model-perm-col">
                <label class="model-form-label">${t("automation.modelLabel")}</label>
                <div class="model-form-dropdown-row">
                  ${renderDropdownImpl("market-add-agent-model-dd", modelOptions, String(agentModelIndex), () => {})}
                </div>
              </div>
              <div class="auto-model-perm-col">
                <label class="model-form-label">${t("input.permissionMode")}</label>
                <div class="model-form-dropdown-row">
                  <div class="settings-dropdown-wrap" data-add="agent-perm-wrap">
                    <button class="settings-dropdown-trigger" data-add="agent-perm-trigger" type="button">
                      <span class="auto-perm-mode-icon" data-add="agent-perm-mode-icon"></span>
                      <span class="auto-perm-mode-label"></span>
                      <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
                    </button>
                    <div class="settings-dropdown" data-add="agent-perm-dropdown"></div>
                  </div>
                </div>
              </div>
            </div>
            <div class="model-form-row">
              <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.systemPrompt")}</label>
              <textarea class="setting-rich-text" data-add="agent-prompt" placeholder="${t("settings.subAgentSystemPromptPlaceholder")}" style="min-height:80px"></textarea>
            </div>
          </div>
          <div class="market-add-form hidden" data-add-body="mcp">
            <div class="model-form-row">
              <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.serverName")}</label>
              <input type="text" class="model-form-input" data-add="mcp-name" placeholder="${t("settings.mcpNamePlaceholder")}" />
            </div>
            <div class="model-form-row">
              <label class="model-form-label">${t("settings.mcpTransport")}</label>
              <div class="model-form-dropdown-row">
                ${renderDropdownImpl("market-add-mcp-transport", transportOptions, "stdio", () => {})}
              </div>
            </div>
            <div class="mcp-transport-fields" data-add="mcp-stdio">
              <div class="model-form-row">
                <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.mcpCommand")}</label>
                <input type="text" class="model-form-input" data-add="mcp-command" placeholder="${t("settings.mcpCommandPlaceholder")}" />
              </div>
              <div class="model-form-row">
                <label class="model-form-label">${t("settings.mcpArgs")}</label>
                <input type="text" class="model-form-input" data-add="mcp-args" placeholder="${t("settings.mcpArgsPlaceholder")}" />
              </div>
            </div>
            <div class="mcp-transport-fields" data-add="mcp-http" style="display:none">
              <div class="model-form-row">
                <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.mcpUrl")}</label>
                <input type="text" class="model-form-input" data-add="mcp-url" placeholder="${t("settings.mcpUrlPlaceholder")}" />
              </div>
            </div>
            <div class="model-form-row" style="flex-direction:column;align-items:stretch">
              <div class="mcp-env-label-row">
                <label class="model-form-label">${t("settings.mcpEnvVars")}</label>
                <button type="button" class="btn-icon" data-add="mcp-add-env" data-tooltip="${t("settings.mcpAddEnv")}">
                  <i data-lucide="plus" class="lucide"></i>
                </button>
              </div>
              <div data-add="mcp-env-list"></div>
            </div>
          </div>
          <div class="market-add-form hidden" data-add-body="zip">
            <div class="market-add-dropzone" data-add-pick>
              <i data-lucide="upload" class="lucide"></i>
              <span class="market-add-drop-hint">${t("market.addZipHint")}</span>
              <span class="market-add-drop-file"></span>
              <input type="file" accept=".zip" class="market-add-file-input" style="display:none" />
            </div>
          </div>
        </div>
        <div class="dialog-footer">
          <button type="button" class="btn" data-add-cancel>${t("common.cancel")}</button>
          <button type="button" class="btn btn--primary" data-add-ok>${t("common.confirm")}</button>
        </div>
      </div>`;

    host.appendChild(editor);
    this.addEditor = editor;
    // The add-editor is a click-away-adjacent floating surface: registering it
    // as ephemeral means every view switch (leaving the market for settings,
    // swapping sessions) closes it through the same door as every popup,
    // instead of it lingering above the view it no longer belongs to.
    registerSurface(editor, {
      scope: "ephemeral",
      onClose: () => { this.addEditor = null; },
    });
    safeCreateIcons(editor);

    const body = editor.querySelector(".dialog-body") as HTMLElement;
    const okBtn = editor.querySelector("[data-add-ok]") as HTMLButtonElement;
    let addType: "agent" | "mcp" | "skill" | "tool" | "core" = "agent";
    let method: "manual" | "zip" | "plugin" = "manual";
    let transport: "stdio" | "http" = "stdio";
    let file: File | null = null;

    // The gate: every kind maps to ONE pipeline, and every path ends in a
    // STANDARD plugin package — a self-contained EA Python package.
    // manual config → packaged by the backend (plugin_install_agent /
    // plugin_install_mcp convert it into a standard package); a plain zip
    // → converted through the standard install pipeline (plugin_install);
    // a plugin package (.whl / .zip) → installed directly (the same
    // plugin_install door, already standard).  skill / tool / core are
    // package-only: their method dropdown collapses to the zip choice.
    const rebuildMethodDd = (): void => {
      const slot = editor.querySelector("[data-add=\"method-dd-slot\"]") as HTMLElement | null;
      if (!slot) return;
      const locked = addType === "skill" || addType === "tool" || addType === "core";
      if (locked && method !== "zip") method = "zip";
      const choices = locked ? methodOptionsFull.filter((o) => o.id === "zip") : methodOptionsFull;
      slot.innerHTML = renderDropdownImpl("market-add-method-dd", choices, method, () => {});
      bindDropdownImpl("market-add-method-dd", (val) => {
        method = val as "manual" | "zip" | "plugin";
        refreshBody();
      });
    };
    const refreshBody = (): void => {
      const manualView = (addType === "agent" || addType === "mcp") && method === "manual";
      const next = manualView ? addType : "zip";
      body.querySelectorAll<HTMLElement>("[data-add-body]").forEach((el) => {
        el.classList.toggle("hidden", el.dataset.addBody !== next);
      });
      // The dropzone names the door: a plain zip is converted to a standard
      // package on install, a plugin package (.whl / .zip) goes in as-is.
      const hint = body.querySelector(".market-add-drop-hint") as HTMLElement | null;
      if (hint) hint.textContent = method === "plugin" ? t("market.addPluginHint") : t("market.addZipHint");
      const picker = body.querySelector(".market-add-file-input") as HTMLInputElement | null;
      if (picker) picker.accept = method === "plugin" ? ".whl,.zip" : ".zip";
      okBtn.textContent = manualView ? t("common.confirm") : t("market.install");
    };
    rebuildMethodDd();
    refreshBody();

    const submit = (): void => {
      const toastErr = (msg: string): void => showToast(t("market.brand"), msg, "error");
      // ZIP path: the package goes through the standard plugin install
      // pipeline (plugin_install) — the same door a manual config's
      // generated manifest package enters through.  skill / tool / core
      // packages land here unconditionally.
      const manualView = (addType === "agent" || addType === "mcp") && method === "manual";
      if (!manualView) {
        if (!file) { toastErr(t("market.addNeedFile")); return; }
        const reader = new FileReader();
        reader.onload = () => {
          const data = String(reader.result || "");
          const b64 = data.includes(",") ? data.slice(data.indexOf(",") + 1) : data;
          send({ type: "plugin_install", data_b64: b64, filename: file!.name });
          showToast(t("market.brand"), t("market.install"));
        };
        reader.readAsDataURL(file);
        this.closeAddPanel();
        return;
      }
      if (addType === "mcp") {
        const name = (body.querySelector("[data-add=\"mcp-name\"]") as HTMLInputElement)?.value?.trim() || "";
        if (!name) { toastErr(t("settings.mcpNameRequired")); return; }
        const server: Record<string, unknown> = { type: transport };
        if (transport === "http") {
          const url = (body.querySelector("[data-add=\"mcp-url\"]") as HTMLInputElement)?.value?.trim() || "";
          if (!url) { toastErr(t("settings.mcpUrlRequired")); return; }
          server.url = url;
          server.timeout = 60;
        } else {
          const command = (body.querySelector("[data-add=\"mcp-command\"]") as HTMLInputElement)?.value?.trim() || "";
          if (!command) { toastErr(t("settings.mcpCommandRequired")); return; }
          server.command = command;
          const argsRaw = (body.querySelector("[data-add=\"mcp-args\"]") as HTMLInputElement)?.value?.trim() || "";
          server.args = argsRaw ? argsRaw.split(/\s+/) : [];
        }
        // A row without a key is dropped (unnamed variables cannot be
        // addressed); an empty VALUE is kept — an unset variable is meaningful.
        const env: Record<string, string> = {};
        body.querySelectorAll<HTMLElement>("[data-add=\"mcp-env-list\"] .mcp-env-row").forEach((row) => {
          const k = (row.querySelector(".mcp-env-key") as HTMLInputElement)?.value?.trim();
          const v = (row.querySelector(".mcp-env-value") as HTMLInputElement)?.value || "";
          if (k) env[k] = v;
        });
        if (Object.keys(env).length > 0) server.env = env;
        send({ type: "plugin_install_mcp", name, server });
        showToast(t("market.brand"), t("market.install"));
        this.closeAddPanel();
        return;
      }
      // agent: the SUB-A form verbatim (same fields, same placeholders, same
      // validation copy as the settings editor) — but gated: the confirmed
      // config is packaged via plugin_install_agent and never touches the
      // settings sub-agents list.
      const name = (body.querySelector("[data-add=\"agent-name\"]") as HTMLInputElement)?.value?.trim() || "";
      const description = (body.querySelector("[data-add=\"agent-desc\"]") as HTMLInputElement)?.value?.trim() || "";
      const prompt = (body.querySelector("[data-add=\"agent-prompt\"]") as HTMLTextAreaElement)?.value || "";
      if (!name) { toastErr(t("settings.subAgentNameRequired")); return; }
      if (!prompt.trim()) { toastErr(t("settings.subAgentPromptRequired")); return; }
      send({
        type: "plugin_install_agent",
        agent: {
          name, description, system_prompt: prompt.trim(),
          model_index: agentModelIndex, permission_mode: agentPerm,
        },
      });
      showToast(t("market.brand"), t("market.install"));
      this.closeAddPanel();
    };

    // Back / cancel dismiss the editor; OK submits through the pipeline the
    // pickers selected.  The two dropdowns drive the body; the MCP transport
    // switch flips its stdio/http field groups.
    editor.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      if (target.closest(".panel-editor-back")) { this.closeAddPanel(); return; }
      if (target.closest("[data-add-cancel]")) { this.closeAddPanel(); return; }
      if (target.closest("[data-add-ok]")) { submit(); return; }
    });
    bindDropdownImpl("market-add-type-dd", (val) => {
      addType = (val === "mcp" || val === "skill" || val === "tool" || val === "core") ? val : "agent";
      // A package-only type collapses the method dropdown to the zip choice
      // (rebuilt, since the option set itself changes).
      rebuildMethodDd();
      refreshBody();
    });

    // MCP transport switch + env rows.
    bindDropdownImpl("market-add-mcp-transport", (val) => {
      transport = (val === "http" ? "http" : "stdio");
      const stdioEl = body.querySelector("[data-add=\"mcp-stdio\"]") as HTMLElement | null;
      const httpEl = body.querySelector("[data-add=\"mcp-http\"]") as HTMLElement | null;
      if (stdioEl) stdioEl.style.display = transport === "http" ? "none" : "";
      if (httpEl) httpEl.style.display = transport === "http" ? "" : "none";
    });
    const envList = body.querySelector("[data-add=\"mcp-env-list\"]") as HTMLElement;
    body.querySelector("[data-add=\"mcp-add-env\"]")?.addEventListener("click", () => {
      const row = document.createElement("div");
      row.innerHTML = envRowHtml();
      const el = row.firstElementChild as HTMLElement;
      envList.appendChild(el);
      el.querySelector<HTMLInputElement>(".mcp-env-key")?.focus();
      safeCreateIcons(el);
    });
    envList.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest(".mcp-env-remove");
      if (btn) (btn.closest(".mcp-env-row") as HTMLElement | null)?.remove();
    });

    // Archive picker: click to browse, drag & drop to drop.
    const dropzone = body.querySelector(".market-add-dropzone") as HTMLElement;
    const fileInput = body.querySelector(".market-add-file-input") as HTMLInputElement;
    const dropFile = body.querySelector(".market-add-drop-file") as HTMLElement;
    dropzone.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const picked = fileInput.files?.[0] || null;
      file = picked;
      dropFile.textContent = picked?.name || "";
    });
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
      const dropped = e.dataTransfer?.files?.[0] || null;
      if (dropped) {
        file = dropped;
        dropFile.textContent = dropped.name;
      }
    });

    // ── Agent extras: execution model + permission mode ──
    // The model picker binds through the shared dropdown pair; the permission
    // picker is the automation dialog's selector, verbatim — shared catalogue
    // + icons (core/permission_mode.ts), group-normalised value, guarded
    // "bypass" switch with the danger confirmation.
    bindDropdownImpl("market-add-agent-model-dd", (val) => {
      const idx = parseInt(val);
      if (!Number.isNaN(idx)) agentModelIndex = idx;
    });
    const agentPermTrigger = body.querySelector("[data-add=\"agent-perm-trigger\"]") as HTMLElement;
    const agentPermDropdown = body.querySelector("[data-add=\"agent-perm-dropdown\"]") as HTMLElement;
    const agentPermLabelEl = agentPermTrigger.querySelector(".auto-perm-mode-label") as HTMLElement;
    const agentPermIconEl = agentPermTrigger.querySelector(".auto-perm-mode-icon") as HTMLElement;
    const paintAgentPermTrigger = () => {
      agentPermLabelEl.textContent = permissionModeLabel(agentPerm);
      agentPermIconEl.innerHTML = `<i data-lucide="${permissionModeIcon(agentPerm)}" class="lucide"></i>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: agentPermIconEl });
      }
    };
    paintAgentPermTrigger();
    const buildAgentPermOptions = () => {
      agentPermDropdown.innerHTML = PERMISSION_MODES.map((m) =>
        `<div class="settings-dropdown-item${m.id === agentPerm ? " selected" : ""}" data-perm-mode="${m.id}"><i data-lucide="${m.icon}" class="lucide"></i><span>${esc(t(m.labelKey))}</span></div>`
      ).join("");
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: agentPermDropdown });
      }
      agentPermDropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          void (async () => {
            // Shared guarded path: "bypass" requires the danger confirmation,
            // rejection keeps the previous selection.
            const applied = await guardPermissionMode(
              (item as HTMLElement).getAttribute("data-perm-mode") || "default",
              dialogPermissionConfirm,
            );
            agentPermDropdown.classList.remove("open");
            if (applied === null) return;
            agentPerm = applied;
            paintAgentPermTrigger();
          })();
        });
      });
    };
    agentPermTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = agentPermDropdown.classList.contains("open");
      if (isOpen) { agentPermDropdown.classList.remove("open"); return; }
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      buildAgentPermOptions();
      agentPermDropdown.classList.add("open");
    });
  }

  /** Dismiss the add editor with the unified exit animation. */
  private closeAddPanel(): void {
    const editor = this.addEditor;
    if (!editor) return;
    this.addEditor = null;
    // Release the scope ownership first so the view-switch closer can never
    // run against a panel that is already animating out.
    unregisterSurface(editor);
    removeAnimated(editor, undefined, "closing");
  }

  // ── Shell & events ────────────────────────────────────────────────

  private buildShell(): void {
    this.built = true;
    this.el.innerHTML = `
      <div class="market">
        <div class="market-page-bar hidden">
          <div class="market-crumb-slot"></div>
          <div class="market-page-tools"></div>
        </div>
        <div class="market-topbar">
          <div class="automation-tabs market-cats"></div>
          <div class="market-top-actions">
            <div class="market-search">
              <i data-lucide="search" class="lucide"></i>
              <input class="market-search-input" type="text" placeholder="${t("market.searchPlaceholder")}" />
            </div>
            ${this.embedded ? "" : `
            <button type="button" class="market-top-btn" data-action="open-installed">${t("market.catInstalled")}</button>
            <button type="button" class="market-top-btn market-top-btn-primary" data-action="add-plugin">${t("market.addPlugin")}</button>`}
          </div>
        </div>
        <div class="market-body"></div>
      </div>
    `;
    this.body = this.el.querySelector(".market-body");
    this.cats = this.el.querySelector(".market-cats");
    this.tabbar = this.el.querySelector(".market-topbar");
    this.crumbBar = this.el.querySelector(".market-crumb-slot");
    this.pageTools = this.el.querySelector(".market-page-tools");
    this.refreshIcons(this.el);
  }

  private bindEvents(): void {
    this.bound = true;
    window.addEventListener("encre-plugin-states", (ev) => {
      this.applyStates((ev as CustomEvent<PluginStateEntry[]>).detail);
      if (this.isOpen) this.renderBody();
    });
    window.addEventListener("encre-market-catalog", (ev) => {
      this.applyCatalog((ev as CustomEvent<{
        source: string;
        index_version: string;
        generated_at: string;
        plugins: MarketCatalogEntry[];
      }>).detail);
      if (this.isOpen) this.renderBody();
    });
    // One skill's own text, answering the request the page made for it.  The
    // card was drawn without it, so redraw once per newly learned skill.
    window.addEventListener("encre-skill-detail", (ev) => {
      const d = (ev as CustomEvent<{
        name: string;
        description?: string;
        body?: string;
        files?: string[];
        error?: string;
      }>).detail;
      if (!d?.name || this.skillDetails.has(d.name)) return;
      this.skillDetails.set(d.name, {
        description: d.description || "",
        body: d.error ? "" : (d.body || ""),
        files: d.error ? [] : (d.files || []),
      });
      if (this.isOpen && this.detailName) this.renderBody();
    });
    // One file inside a skill package, answering the reader's walk.  The reply
    // lands on the card that asked for it, wherever in the trail it is.
    window.addEventListener("encre-skill-file", (ev) => {
      const d = (ev as CustomEvent<{
        name: string;
        path: string;
        content?: string;
        error?: string;
      }>).detail;
      if (!d?.name || !d.path) return;
      const key = `${d.name}::${d.path}`;
      this.skillFilePending.delete(key);
      if (d.error) this.skillFileErrors.add(key);
      else this.skillFiles.set(key, d.content || "");
      if (this.skillReader) this.renderSkillReader();
    });
    window.addEventListener("encre-plugin-lifecycle-ok", (ev) => {
      const d = (ev as CustomEvent<{ type: string; name: string }>).detail;
      if (d.name) {
        this.endOp(d.name);
        return;
      }
      if (this.isOpen) this.renderBody();
    });
    window.addEventListener("encre-plugin-lifecycle-error", (ev) => {
      const d = (ev as CustomEvent<{ type: string; name?: string; message: string }>).detail;
      if (this.isOpen) showToast(t("market.brand"), d.message || t("market.opFailed"));
      if (d.name) this.endOp(d.name);
      else if (this.isOpen) this.renderBody();
    });
  }

  /** Mark an operation in flight and remember when its bar started running. */
  private beginOp(name: string, op: PendingOp): void {
    const started = Date.now();
    this.pending.set(name, op);
    this.pendingSince.set(name, started);
    this.repaintOwed = false;
    // A lost lifecycle reply would otherwise hold the repaint forever.
    setTimeout(() => {
      if (this.pendingSince.get(name) === started) this.endOp(name);
    }, PROGRESS_WATCHDOG_MS);
    // This is the frame the bar starts on, so it is never held back — but it
    // still paints through the scheduler's immediate mode (one path, one
    // boundary).
    runScheduledRenderNow("market", () => {
      if (this.isOpen) this.paintBody();
    }, 50);
  }

  /** Clear an operation once its bar has completed one sweep.  The lifecycle
   *  reply can land before the animation has run, and a half-shown bar reads
   *  as a glitch rather than as work that happened. */
  private endOp(name: string): void {
    const started = this.pendingSince.get(name);
    const remain = started === undefined ? 0 : PROGRESS_HOLD_MS - (Date.now() - started);
    if (remain <= 0) {
      this.finishOp(name);
      return;
    }
    setTimeout(() => this.finishOp(name), remain);
  }

  /** Drop the in-flight mark and repaint whatever the snapshot now says. */
  private finishOp(name: string): void {
    this.pending.delete(name);
    this.pendingSince.delete(name);
    if (this.isOpen) this.renderBody();
  }

  /** Take in one local-registry snapshot.  `null` means "nothing arrived". */
  private applyStates(states: PluginStateEntry[] | null): void {
    if (!states) return;
    this.states = states;
    this.itemsCache = null;
    this.statesArrived = true;
  }

  /** Take in one central-index frame. */
  private applyCatalog(d: {
    source?: string;
    index_version?: string;
    generated_at?: string;
    plugins?: MarketCatalogEntry[];
  } | null): void {
    if (!d) return;
    this.central = d.plugins || [];
    this.centralSource = d.source || "none";
    this.indexVersion = d.index_version || "";
    this.generatedAt = d.generated_at || "";
    this.itemsCache = null;
    this.catalogArrived = true;
  }

  /**
   * Adopt what the backend already delivered before this view existed.
   *
   * Both planes arrive as one-shot frames — the registry snapshot on connect
   * and after warmup, the index alongside them — while this view can only be
   * opened by a click, i.e. strictly after boot.  Reading the shared stores
   * here is what stops an open market from waiting forever on a push that
   * already happened, which is exactly how it got stuck on "loading".
   *
   * It also picks the honest landing tab: a curated index that has published
   * nothing yet is not a storefront, so the market opens on what this machine
   * actually has instead of an empty shelf.  Nothing is invented to fill it.
   */
  private hydrate(): void {
    this.applyStates(pluginStates());
    const index = marketIndex();
    this.applyCatalog(index
      ? {
        source: index.source,
        index_version: index.indexVersion,
        generated_at: index.generatedAt,
        plugins: index.plugins,
      }
      : null);
  }

  /**
   * The top bar's category strip — the Automation view's own tab control
   * (`.automation-tabs` / `.automation-tab`), the same one the settings 插件
   * panel uses, so both surfaces switch categories with one component.
   */
  private renderCats(): void {
    if (!this.cats) return;
    const defs: { key: MarketGroup | "all"; label: string }[] = [
      { key: "all", label: t("sidebar.plugins") },
      ...this.catDefs().map((r) => ({ key: r.key, label: r.label })),
    ];
    this.cats.innerHTML = defs.map((d) => `
      <button type="button" class="automation-tab${d.key === this.category ? " active" : ""}"
        data-action="category" data-group="${d.key}">${d.label}</button>`).join("");
  }

  /**
   * The crumb band above a plugin page: 插件 / <name>.
   *
   * Built by app's own crumb component (the one the data center pages and
   * the session bar use), so the trail, its separator and its truncation look
   * identical everywhere.  Its root is the single way back out of a plugin.
   *
   * At the row's far end sit the plugin's own links, then the ⋮ that holds its
   * destructive controls (see :meth:`openMoreMenu`).  Both are the row's own
   * icon-button dialect — and the ⋮ keeps its place while an operation runs,
   * because the bar belongs to the action column, not here.
   */
  private showCrumb(a: MarketItem): void {
    if (!this.crumbBar) return;
    this.crumbBar.closest(".market-page-bar")?.classList.remove("hidden");
    renderCrumbs(this.crumbBar, [
      { label: t("market.crumbRoot"), onActivate: () => this.closeDetail() },
      // Same user-facing rule as the card and detail title: show the
      // display name, falling back to the package name when blank.
      { label: a.displayName || a.name },
    ]);
    if (this.pageTools) {
      const links = this.linksOf(a).map(([icon, href]) => `
        <a class="btn-icon" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(href)}">
          <i data-lucide="${icon}" class="lucide"></i>
        </a>`).join("");
      this.pageTools.innerHTML = `${links}${a.local && a.local.tier !== "mandatory" ? `
        <button type="button" class="btn-icon" data-action="more">
          <i data-lucide="more-horizontal" class="lucide"></i>
        </button>` : ""}`;
      this.refreshIcons(this.pageTools);
    }
  }

  /** The support channels the plugin declares, as [glyph, href] in the order
   *  they are shown: official site, repository, docs, then e-mail.  A plugin
   *  that declares nothing contributes nothing here. */
  private linksOf(a: MarketItem): Array<[string, string]> {
    return [
      a.homepage && ["globe", a.homepage],
      a.repository && ["git-branch", a.repository],
      a.docs && ["book-open", a.docs],
      a.email && ["mail", `mailto:${a.email}`],
    ].filter(Boolean) as Array<[string, string]>;
  }

  /**
   * Open the ⋮ menu: 停用/启用 first (the label and the power glyph flip with
   * the current state), then 卸载 in the danger tint.  Both actions ask on the
   * app's yes/no card before anything is sent.
   *
   * This is the application menu's own vocabulary — the `.encre-menu-dropdown`
   * panel, `.encre-menu-item-row` rows, `.encre-menu-item-divider`, anchored by
   * `showAnchoredDropdown` and dismissed through the shared backdrop — so the
   * app does not grow a third menu dialect.  `.encre-menu-item-danger` is that
   * dialect's one addition, the red row the context menu already had.
   */
  private openMoreMenu(trigger: HTMLElement): void {
    if (this.moreMenu) {
      this.closeMoreMenu();
      return;
    }
    const item = this.detailName ? this.find(this.detailName) : undefined;
    const local = item?.local;
    // Core (mandatory) plugins carry no lifecycle: they cannot be switched
    // off or removed, so the menu that only offers those is not drawn.
    if (!local || local.tier === "mandatory") return;
    const enabled = local.enabled;
    const name = item.name;
    const row = (menu: string, icon: string, label: string, danger = false) => `
        <button type="button" class="encre-menu-item-row encre-menu-action-row${danger ? " encre-menu-item-danger" : ""}" data-menu="${menu}">
          <div class="encre-menu-item-info">
            <div class="encre-menu-item-title"><i data-lucide="${icon}" class="lucide"></i>${esc(label)}</div>
          </div>
        </button>`;

    // Only one such panel exists at a time app-wide (the backdrop is an id,
    // so a second one would collide with the application menu's own).
    document.querySelectorAll(".encre-menu-dropdown, #encre-menu-backdrop").forEach((n) => n.remove());

    const menu = document.createElement("div");
    menu.className = "encre-menu-dropdown";
    menu.innerHTML = `<div class="encre-menu-main">${
      row("toggle", enabled ? "power-off" : "power",
          enabled ? t("market.actionDisable") : t("market.actionEnable"))
    }<div class="encre-menu-item-divider"></div>${
      row("uninstall", "trash-2", t("market.uninstall"), true)
    }</div>`;
    document.body.appendChild(menu);
    this.refreshIcons(menu);
    showAnchoredDropdown(menu, trigger);

    menu.addEventListener("click", (e) => {
      e.stopPropagation();
      const rowEl = (e.target as HTMLElement).closest<HTMLElement>("[data-menu]");
      if (!rowEl) return;
      this.closeMoreMenu();
      if (rowEl.dataset.menu === "toggle") this.setPluginEnabled(name, !enabled);
      else if (rowEl.dataset.menu === "uninstall") this.uninstall(name);
    });

    const backdrop = document.createElement("div");
    backdrop.id = "encre-menu-backdrop";
    document.body.appendChild(backdrop);
    backdrop.addEventListener("click", () => this.closeMoreMenu());

    this.moreMenu = menu;
    this.moreBackdrop = backdrop;
  }

  /** Take the page menu and its backdrop away, through the menu's own exit
   *  animation so it leaves the way the application menu does. */
  private closeMoreMenu(): void {
    const menu = this.moreMenu;
    const backdrop = this.moreBackdrop;
    this.moreMenu = null;
    this.moreBackdrop = null;
    if (!menu) return;
    const cleanup = (): void => {
      menu.remove();
      backdrop?.remove();
    };
    menu.classList.add("closing");
    backdrop?.classList.add("closing");
    menu.addEventListener("animationend", cleanup, { once: true });
    setTimeout(cleanup, 600);
  }

  /** Persist the enabled flag and repaint; the snapshot reply does the rest.
   *  Turning a plugin off takes its skills and tools out of the running loop,
   *  so it asks first; turning one back on needs no asking. */
  private async setPluginEnabled(name: string, enabled: boolean): Promise<void> {
    if (this.pending.has(name)) return;
    if (!enabled && !(await this.ask(t("market.actionDisable"), t("market.disableConfirm", { name })))) return;
    this.beginOp(name, enabled ? "enable" : "disable");
    send({ type: enabled ? "plugin_enable" : "plugin_disable", name });
  }

  /** Remove the plugin package; the lifecycle reply repaints the page. */
  private async uninstall(name: string): Promise<void> {
    if (this.pending.has(name)) return;
    if (!(await this.ask(t("market.uninstall"), t("market.uninstallConfirm", { name })))) return;
    this.beginOp(name, "uninstall");
    send({ type: "plugin_uninstall", name });
  }

  /** The app's own yes/no card, in the same container every other destructive
   *  action uses.  Resolves false when the user backs out. */
  private ask(title: string, body: string): Promise<boolean> {
    return Dialog.confirm(title, body, {
      scope: "app",
    });
  }

  /** Leave the plugin page and show the list again. */
  private closeDetail(): void {
    if (!this.detailName) return;
    this.detailName = null;
    this.closeSkillReader();
    this.renderBody();
  }

  // ── Data helpers ──────────────────────────────────────────────────

  /** The merged store: local registry entries ∪ central index entries.
   *  Core (mandatory tier) rides along — it is browsable under its own
   *  section but offers no lifecycle controls.  Yanked index rows stay
   *  visible only where something is already installed.  Tab semantics
   *  (see :meth:`filtered`): 发现 browses the central index exclusively,
   *  已安装 browses what is installed locally. */
  /** Merged view cache. `items()` is consulted several times per repaint
   *  (hero, chips, list); rebuilding the ~250-entry merge each time was pure
   *  work. Invalidated whenever either data plane is replaced. */
  private itemsCache: MarketItem[] | null = null;

  private items(): MarketItem[] {
    if (this.itemsCache) return this.itemsCache;
    const byName = new Map<string, MarketItem>();
    for (const p of this.states) {
      byName.set(p.name, {
        name: p.name,
        version: p.version || "",
        description: p.description || "",
        displayName: p.display_name || "",
        author: p.author || "",
        icon: p.icon || "",
        tags: p.tags || [],
        permissions: p.permissions || [],
        resources: p.resources || {},
        uiPermissions: p.ui_permissions || {},
        provides: p.provides || {},
        homepage: p.homepage || "",
        repository: p.repository || "",
        docs: p.docs || "",
        email: p.email || "",
        local: p,
        installed: !!p.installed,
        updatable: false,
      });
    }
    for (const c of this.central) {
      const it = byName.get(c.name);
      if (!it) {
        if (c.yanked) continue;
        byName.set(c.name, {
          name: c.name,
          version: c.version || "",
          description: c.description || "",
          displayName: c.display_name || "",
          author: c.author || "",
          icon: c.icon || "",
          tags: c.tags || [],
          permissions: c.permissions || [],
          resources: {},
          uiPermissions: {},
          provides: c.provides || {},
          homepage: c.homepage || "",
          repository: c.repository || "",
          docs: c.docs || "",
          email: c.email || "",
          central: c,
          installed: false,
          updatable: false,
        });
        continue;
      }
      it.central = c;
      // The installed copy's own manifest is the truth for what is on
      // disk; the index only fills fields the local manifest left blank.
      const pick = (cur: string, next: string) => cur || next;
      it.description = pick(it.description, c.description || "");
      it.displayName = pick(it.displayName, c.display_name || "");
      it.author = pick(it.author, c.author || "");
      it.icon = pick(it.icon, c.icon || "");
      it.homepage = pick(it.homepage, c.homepage || "");
      it.repository = pick(it.repository, c.repository || "");
      it.docs = pick(it.docs, c.docs || "");
      it.email = pick(it.email, c.email || "");
      if (!it.tags.length) it.tags = c.tags || [];
      if (!it.permissions.length) it.permissions = c.permissions || [];
      if (!Object.keys(it.provides).length) it.provides = c.provides || {};
      it.updatable =
        it.installed && !c.yanked && vcmp(c.version || "", it.local?.version || "") > 0;
    }
    this.itemsCache = [...byName.values()];
    return this.itemsCache;
  }

  private find(name: string): MarketItem | undefined {
    return this.items().find((p) => p.name === name);
  }

  /** The category strip's shelves, in reading order.  MCP reuses the app's
   *  word for it (连接器) rather than inventing a second name for the same
   *  thing. */
  private catDefs(): { key: MarketGroup; label: string; icon: string }[] {
    return [
      { key: "skill", label: t("market.surfaceSkills"), icon: "sparkles" },
      { key: "mcp", label: t("market.surfaceMcp"), icon: "server" },
      { key: "agent", label: t("market.catAgent"), icon: "bot" },
      { key: "tool", label: t("market.surfaceTools"), icon: "wrench" },
      { key: "core", label: t("market.catCore"), icon: "package" },
    ];
  }

  /**
   * What this instance sells: the central index (remote store) or this
   * machine's installed packages (settings 插件 panel).  One view, two data
   * planes — a locally installed entry that IS in the index keeps its
   * 已安装 badge on the remote side.
   */
  private filteredBase(): MarketItem[] {
    const q = this.query;
    let apps = this.mode === "installed"
      ? this.items().filter((p) => p.installed)
      : this.items().filter((p) => !!p.central && !p.central.yanked);
    if (q) {
      apps = apps.filter((p) =>
        `${p.name} ${p.displayName} ${p.description} ${p.author} ${p.tags.join(" ")}`
          .toLowerCase()
          .includes(q));
    }
    // Installed-first reads like a store ("your" apps surface), updatable
    // next so an available update is never buried, then alphabetical for
    // stable browsing.
    return [...apps].sort((a, b) =>
      (b.installed ? 1 : 0) - (a.installed ? 1 : 0)
      || (b.updatable ? 1 : 0) - (a.updatable ? 1 : 0)
      || a.name.localeCompare(b.name));
  }

  // ── Rendering ─────────────────────────────────────────────────────

  /** Ask for a repaint.  Held while a lifecycle operation is in flight: a
   *  snapshot landing mid-operation would rebuild the bar (restarting its
   *  fill) and paint the new state before the fill has run, so the repaint is
   *  remembered and done once, when the bar retires.  Held the same way while
   *  the skill reader is open, so closing it reveals the page left as it was. */
  private renderBody(): void {
    if (this.pending.size > 0 || this.skillReader) {
      this.repaintOwed = true;
      return;
    }
    this.repaintOwed = false;
    // Coalesced with every other surface: one market paint per frame even
    // when a snapshot and a lifecycle event land in the same tick.
    scheduleRender("market", () => {
      if (!this.isOpen) return;
      // A lifecycle operation may have started after this paint was queued;
      // hold it until the operation retires, exactly like a direct call would.
      if (this.pending.size > 0 || this.skillReader) {
        this.repaintOwed = true;
        return;
      }
      this.paintBody();
    }, 50);
  }

  /** Identifier of the view whose markup is currently painted. A repaint of
   *  the same view preserves its scroll; switching view starts at the top. */
  private paintedView = "";

  /** Swap the body markup without losing the reader's place. */
  private paintBodyHtml(html: string, viewKey: string): void {
    if (!this.body) return;
    const prevTop = this.body.scrollTop;
    const sameView = this.paintedView === viewKey;
    this.paintedView = viewKey;
    this.body.innerHTML = html;
    this.body.scrollTop = sameView ? prevTop : 0;
    this.refreshIcons(this.body);
  }

  private paintBody(): void {
    if (!this.body) return;
    if (this.detailName) {
      const app = this.find(this.detailName);
      if (app) {
        if (this.tabbar) this.tabbar.classList.add("hidden");
        this.showCrumb(app);
        this.paintBodyHtml(
          guardHtml(
            "market-detail",
            this.detailName,
            () => this.renderDetail(app),
            `<div class="si-empty-center"><span class="si-empty-title">${esc(this.detailName)}</span></div>`,
          ),
          `detail:${this.detailName}`,
        );
        this.requestSkills(app.provides.skills || []);

        return;
      }
      this.detailName = null; // vanished from the snapshot — fall back
    }
    if (this.tabbar) this.tabbar.classList.remove("hidden");
    this.closeMoreMenu();
    this.crumbBar?.closest(".market-page-bar")?.classList.add("hidden");
    if (this.pageTools) this.pageTools.innerHTML = "";
    // Each data plane gates its own first paint: the store waits for the
    // index, the installed view waits for the registry snapshot.  The
    // category strip is drawn anyway — which shelves exist never depends on
    // data that has not landed yet.
    this.renderCats();
    if (this.mode === "installed" ? !this.statesArrived : !this.catalogArrived) {
      // Genuinely still booting: this data plane has not spoken yet.
      // The indeterminate bar alone means "waiting on the backend" — no
      // extra icon or caption next to it.
      this.paintBodyHtml(
        `<div class="si-empty-center">${progressBar("indeterminate")}</div>`,
        "loading",
      );
      return;
    }
    this.renderList();
  }

  /**
   * The store: a category rail on the left and the grid of the chosen
   * category on the right.  Picking a shelf replaces the grid instead of
   * scrolling past the other shelves — 工具 is one click from 技能, never a
   * 185-row scroll away.  Counts come from this tab's own data plane, and
   * when the active shelf has nothing here (tab switch, search) the first
   * non-empty one takes over so the page never opens on an empty shelf.
   */
  private renderList(): void {
    if (!this.body) return;
    const base = this.filteredBase();
    this.renderCats();
    let rows = this.category === "all" ? base : base.filter((a) => groupOf(a) === this.category);
    if (this.tagFilter) rows = rows.filter((a) => (a.tags || []).includes(this.tagFilter));
    this.list = rows;
    // The store front: hero (homepage only — the settings 插件 panel embeds
    // the same list without the banner), tag chips, then the grid.
    const hero = this.mode === "remote" ? this.renderHero() : "";
    const chips = this.renderChips(base);
    let grid = "";
    if (!rows.length) {
      // Two honest states only: a search that matched nothing, or a shelf
      // that is empty.  No explainer copy about where the data comes from —
      // the page never lectures the user about its own plumbing.
      grid = this.query || this.tagFilter
        ? `<div class="si-empty-center">
            <i data-lucide="search-x" class="lucide"></i>
            <span class="si-empty-title">${t("market.noResults")}</span>
          </div>`
        : `<div class="si-empty-center">
            <i data-lucide="layout-grid" class="lucide"></i>
            <span class="si-empty-title">${this.mode === "installed" ? t("market.emptyInstalled") : t("market.emptyMarket")}</span>
          </div>`;
    } else {
      // One plugin's malformed metadata must not blank the store: a failed
      // card is replaced by a minimal card carrying only its own name.
      grid = `<div class="market-grid">${rows.map((a) => guardHtml(
        "market-card",
        a.name,
        () => this.renderCard(a),
        `<div class="market-app"><div class="market-app-name">${esc(a.name)}</div></div>`,
      )).join("")}</div>`;
    }
    // Category/tab/search changes are a new view (start at the top); a
    // repaint caused by a data-plane refresh keeps the scroll position.
    this.paintBodyHtml(
      `${hero}${chips}${grid}`,
      `list:${this.mode}:${this.category}:${this.tagFilter}:${this.query}`,
    );
  }

  /**
   * The store's own front page, re-cut for the active category: the headline
   * names the shelf in its own colour, and the art carries that shelf's own
   * icons — fixed bundled residents where a shelf has them, the shelf's own
   * entries where it does not — so each tab's banner belongs to that tab.
   */
  private renderHero(): string {
    const brand = HERO_BRAND[this.category];
    // Residents when the shelf has them; otherwise the shelf's own entries
    // (a connector shelf's banner shows connectors). Looked up by name in
    // the merged plane, so each glyph is a plugin's declared icon; a slot
    // whose icon cannot resolve pads with a plain glyph, never goes empty.
    const residents = HERO_RESIDENTS[this.category];
    const byName = new Map(this.items().map((it) => [it.name, it]));
    const names = residents ?? this.items()
      .filter((it) => !!it.central && !it.central.yanked && groupOf(it) === this.category)
      .slice(0, 5).map((it) => it.name);
    const pads = ["bot", "globe", "image", "database", "mail"];
    const art = names.map((name, i) => {
      const icon = byName.get(name)?.icon;
      const html = icon ? pluginIconHtml(icon, name, { emojiClass: "market-emoji" }) : "";
      return `<span class="market-hero-glyph" data-i="${i}">${html || `<i data-lucide="${pads[i]}" class="lucide"></i>`}</span>`;
    }).join("");
    return `<div class="market-hero">
      <div class="market-hero-title">${t("market.discover")} <span class="market-hero-brand" style="color:${brand.color}">${brand.label}</span></div>
      <div class="market-hero-art" aria-hidden="true">${art}</div>
    </div>`;
  }

  /**
   * Tag chips: 全部 plus the tags the catalogue actually repeats most (the
   * curated index declares them), so the filter row describes the shelf
   * rather than a taxonomy the store invented.
   */
  private renderChips(base: MarketItem[]): string {
    const freq = new Map<string, number>();
    for (const a of base) {
      for (const raw of a.tags || []) {
        const tag = String(raw || "").trim();
        if (tag.length < 2 || tag.length > 18) continue;
        if (GENERIC_TAGS.has(tag.toLowerCase())) continue;
        freq.set(tag, (freq.get(tag) || 0) + 1);
      }
    }
    const tags = [...freq.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 8)
      .map((e) => e[0]);
    if (!tags.length) return "";
    const chip = (label: string, value: string) => `
      <button type="button" class="market-chip${this.tagFilter === value ? " active" : ""}"
        data-action="tag" data-tag="${esc(value)}">${esc(label)}</button>`;
    return `<div class="market-chips">${chip(t("market.chipAll"), "")}${tags.map((tg) => chip(tg, tg)).join("")}</div>`;
  }

  /** One store row: the plugin's own tile, its name, one line of what it does.
   *  The row is a link into the detail page only — installing, updating and
   *  enabling live there, so a plugin never has two places to act on it. */
  private renderCard(a: MarketItem): string {
    const off = a.local !== undefined && !a.local.enabled;
    return `
      <div class="market-app" data-action="detail" data-name="${esc(a.name)}">
        ${a.updatable ? `<span class="market-badge">${t("market.updatable")}</span>` : ""}
        <span class="market-tile market-tile-sm${off ? " market-tile-off" : ""}">${itemIconHtml(a)}${off ? this.offBadgeHtml() : ""}</span>
        <span class="market-app-text">
          <span class="market-app-name">${esc(a.displayName || a.name)}</span>
          <span class="market-app-desc">${esc(a.description || "")}</span>
        </span>
        ${this.renderAddButton(a)}
      </div>`;
  }

  /**
   * The one marker that says a plugin was switched off: a badge pinned to the
   * bottom-right corner of the plugin's own icon — the tile is only the plate
   * the icon sits on, so the state rides on the icon itself and the row keeps
   * no slot of its own for it.  The badge is a glyph, not a label; the tooltip
   * carries the word itself.
   */
  private offBadgeHtml(): string {
    const label = t("market.disabled");
    return `<span class="market-off-badge" data-tooltip="${esc(label)}" aria-label="${esc(label)}">
        <i data-lucide="power-off" class="lucide"></i>
      </span>`;
  }

  /**
   * The row's hover-only "+" — the one action a store front offers without
   * opening the plugin first.  A plugin that is already installed and current
   * has nothing to add, so no button appears for it.  While an operation on it
   * is in flight the "+" gives way to the progress bar.
   */
  private renderAddButton(a: MarketItem): string {
    if (this.pending.has(a.name)) return progressBar("completing");
    if (a.installed && !a.updatable) return "";
    const label = a.updatable ? t("market.updatable") : t("market.install");
    return `<button type="button" class="market-add" data-action="install" data-name="${esc(a.name)}" data-tooltip="${esc(label)}" aria-label="${esc(label)}">
      <i data-lucide="plus" class="lucide"></i></button>`;
  }

  /** The plugin's install / update control.  A plugin that is already current
   *  gets no pill at all — its switch and 卸载 say everything. */
  private renderActionPill(a: MarketItem): string {
    if (a.updatable) {
      // Same `market_install` frame: the backend sees the index pins a
      // newer version and pulls the pinned wheel instead of re-activating.
      return `<button type="button" class="market-pill market-pill-wide market-pill-go" data-action="install" data-name="${esc(a.name)}">
        ${t("market.updatable")}</button>`;
    }
    return `<button type="button" class="market-pill market-pill-wide market-pill-go" data-action="install" data-name="${esc(a.name)}">
      ${t("market.install")}</button>`;
  }

  /**
   * The plugin's page: a breadcrumb that doubles as the single way back, a
   * header carrying its identity plus the one action column, then the
   * capabilities it declares — skills, tools, connectors — and its permission
   * list.  Every string on it is the plugin's declaration or a file it ships —
   * the store writes no copy of its own.
   */
  private renderDetail(a: MarketItem): string {
    const local = a.local;
    const version = (local ? local.version : a.central?.version) || a.version;
    const skills = a.provides.skills || [];
    const tools = a.provides.tools || [];
    const mcp = a.provides.mcp || [];
    // The declared `permissions` list is one field carrying two vocabularies:
    // registration surfaces (``tools:register``, ``loop:intercept``) and
    // resource grants (``filesystem:write``, ``network``).  Filing them by
    // MEANING — not by which manifest field they came from — is what makes
    // the page honest: a reader sees what may be touched, and separately what
    // the plugin may register into the app.
    const declared = a.permissions || [];
    const isResourceToken = (p: string) =>
      /^(filesystem|network|process|shell|clipboard|screen|memory|session|config|environment|system_info|llm_call)/.test(p.trim().toLowerCase());
    const surfaceTokens = declared.filter((p) => !isResourceToken(p));
    const resourceStrings = declared.filter(isResourceToken);
    const resourceKeys = [...new Set([...Object.keys(a.resources || {}), ...resourceStrings])];
    const otherProvides = Object.entries(a.provides)
      .filter(([surface, values]) => !["skills", "tools", "mcp"].includes(surface) && (values || []).length > 0)
      .flatMap(([surface, values]) => (values || []).map((v) => `${surface}: ${v}`));
    // The header column holds only what you can still do to the plugin:
    // 安装, or 更新 when the index pins a newer version.  A plugin that is on
    // this machine and current has nothing to offer here — its 停用 / 卸载 live
    // in the ⋮ at the end of the crumb row.  While any operation is in flight
    // the column shows the progress bar instead of a control.
    const action = this.pending.has(a.name)
      ? progressBar("completing")
      : !a.installed || a.updatable ? this.renderActionPill(a) : "";
    // Two lines and nothing else: the name with its version, then who made it
    // and one line of what it does, the two held apart by the line's own gap.
    // The author's e-mail is the tooltip on the name, not a second word.
    const summary = a.description ? `<span class="market-summary">${esc(a.description)}</span>` : "";
    return `
      <div class="market-detail">
      <div class="market-detail-head">
        <span class="market-tile${local && !local.enabled ? " market-tile-off" : ""}">${itemIconHtml(a)}${local && !local.enabled ? this.offBadgeHtml() : ""}</span>
        <div class="market-detail-heading">
          <div class="market-detail-name">${esc(a.displayName || a.name)}<span class="market-version">${esc(version)}</span></div>
          <div class="market-detail-author">${this.authorHtml(a.author)}${summary}</div>
        </div>
        <div class="market-detail-actions">${action}</div>
      </div>
      <div class="market-detail-body">
        ${skills.length ? `
          <div class="market-sec-title">${t("market.surfaceSkills")}<span class="market-sec-count">${skills.length}</span></div>
          ${skills.map((s) => this.skillOffer(s)).join("")}` : ""}
        ${tools.length && !isToolOnlyPackage(a) ? `
          <div class="market-sec-title">${t("market.surfaceTools")}<span class="market-sec-count">${tools.length}</span></div>
          ${tools.map((s) => this.toolOffer(a.name, s)).join("")}` : ""}
        ${mcp.length ? `
          <div class="market-sec-title">${t("market.surfaceMcp")}<span class="market-sec-count">${mcp.length}</span></div>
          ${mcp.map((s) => this.mcpOffer(s)).join("")}` : ""}
        ${otherProvides.length ? `
          <div class="market-sec-title">${t("market.capabilities")}<span class="market-sec-count">${otherProvides.length}</span></div>
          <div class="market-cap-grid">${otherProvides.map((p) => `<span class="market-cap">${esc(p)}</span>`).join("")}</div>` : ""}
        ${surfaceTokens.length ? `
          <div class="market-sec-title">${t("market.detailPermissions")}<span class="market-sec-count">${surfaceTokens.length}</span></div>
          ${surfaceTokens.map((p) => this.permRow(p)).join("")}` : ""}
        ${resourceKeys.length ? `
          <div class="market-sec-title">${t("market.detailResourcePerms")}<span class="market-sec-count">${resourceKeys.length}</span></div>
          ${resourceKeys.map((k) => (k in (a.resources || {})
            ? this.grantRows({ [k]: (a.resources || {})[k] })
            : this.permRow(k))).join("")}` : ""}
        ${Object.keys(a.uiPermissions).length ? `
          <div class="market-sec-title">${t("market.detailUiPerms")}<span class="market-sec-count">${Object.keys(a.uiPermissions).length}</span></div>
          ${this.grantRows(a.uiPermissions)}` : ""}
      </div>
      </div>`;
  }

  /**
   * One grant, stated three ways at once: the capability's glyph, the plain
   * label with its raw id underneath, and the risk tier at the right.  The
   * tier colours the row's edge so a scan down the column reads as a scale.
   */
  private permRow(id: string): string {
    const meta = permMeta(id);
    const riskKey = meta.risk === "low"
      ? "market.riskLow"
      : meta.risk === "high" ? "market.riskHigh" : "market.riskMid";
    return this.grantCard(meta.icon, t(meta.labelKey), esc(id), meta.risk, t(riskKey));
  }

  /** The engine's grant layers, one row per key: the identifier as the title
   *  (it is the API vocabulary, so it is not translated), its allowance
   *  underneath, tier at the trailing edge. */
  private grantRows(grants: Record<string, unknown>): string {
    return Object.entries(grants).map(([key, value]) => {
      const meta = grantMeta(key);
      const riskKey = meta.risk === "low"
        ? "market.riskLow"
        : meta.risk === "high" ? "market.riskHigh" : "market.riskMid";
      return this.grantCard(meta.icon, esc(key), esc(grantValueText(value)), meta.risk, t(riskKey));
    }).join("");
  }

  /**
   * One permission, drawn as the SAME row a skill / tool / connector uses:
   * glyph, title, second line, and one thing at the trailing edge.  Skills
   * put a launcher arrow there; a grant puts its risk tier, quiet text rather
   * than a second badge.  One card dialect for the whole detail page.
   */
  private grantCard(icon: string, title: string, line: string, risk: string, riskLabel: string): string {
    return `
      <div class="settings-card market-offer-row">
        <span class="market-offer-icon"><i data-lucide="${icon}" class="lucide"></i></span>
        <span class="market-offer-text">
          <span class="market-offer-title">${title}</span>
          ${line ? `<span class="market-offer-desc">${line}</span>` : ""}
        </span>
        <span class="market-perm-tag market-perm-tag-${risk}">${riskLabel}</span>
      </div>`;
  }

  /** The declared author, split into the name shown and the e-mail tooltip. */
  private authorHtml(author: string): string {
    const raw = (author || "").trim();
    if (!raw) return "";
    const m = raw.match(/^(.*?)\s*<([^<>@]+@[^<>]+)>$/);
    const name = (m ? m[1] : raw).trim();
    const email = m ? m[2].trim() : "";
    const tip = email ? ` data-tooltip="${esc(email)}"` : "";
    return `<span class="market-author"${tip}>@${esc(name || email)}</span>`;
  }

  /**
   * One skill the plugin provides: a card with its name and description, and
   * the launcher's corner ↗ that opens the skill's own SKILL.md in the reader
   * card.
   *
   * The text is the skill registry's — the same ``SkillInfo`` the settings
   * skills page and the chat command palette read — so the market never
   * paraphrases a plugin's skill.  A declared skill whose body the registry has
   * not answered (a disabled plugin, for instance) gets no arrow: there is
   * nothing there to read, so the card claims no way in.
   */
  private skillOffer(name: string): string {
    const known = this.skillDetails.get(name);
    const desc = known?.description || "";
    return `
      <div class="settings-card market-offer-row" data-skill="${esc(name)}">
        <span class="market-offer-icon"><i data-lucide="wand-2" class="lucide"></i></span>
        <span class="market-offer-text">
          <span class="market-offer-title">${esc(name)}</span>
          ${desc ? `<span class="market-offer-desc">${esc(desc)}</span>` : ""}
        </span>
        ${known?.body ? `
        <button type="button" class="btn-icon" data-action="skill" data-skill="${esc(name)}" data-tooltip="${esc(t("market.viewSkill"))}">
          <i data-lucide="arrow-up-right" class="lucide"></i>
        </button>` : ""}
      </div>`;
  }

  /** Ask for the skills of the page just rendered that we do not have yet. */
  private requestSkills(names: string[]): void {
    for (const name of names) {
      if (this.skillDetails.has(name)) continue;
      send({ type: "get_skill_detail", name });
    }
  }

  /** One tool the plugin provides, drawn with the tool's own declared label
   *  and glyph — this app has no tool description to show, so none is invented. */
  private toolOffer(plugin: string, name: string): string {
    const meta = metaFor(name);
    const label = meta.label && meta.label !== name ? meta.label : "";
    return `
      <div class="settings-card market-offer-row">
        <span class="market-offer-icon">${toolIconHtml(name)}</span>
        <span class="market-offer-text">
          <span class="market-offer-title">${esc(name)}</span>
          ${label ? `<span class="market-offer-desc">${esc(label)}</span>` : ""}
        </span>
      </div>`;
  }

  /** One MCP server the plugin contributes: transport and endpoint, the way
   *  the MCP settings list describes it. */
  private mcpOffer(name: string): string {
    const srv = getState().mcpServers.find((s) => s.name === name);
    let detail = "";
    if (srv) {
      detail = srv.type === "http"
        ? `HTTP · ${srv.url || ""}`
        : `stdio · ${[srv.command || "", ...(srv.args || [])].join(" ").trim()}`;
    }
    return `
      <div class="settings-card market-offer-row">
        <span class="market-offer-icon"><i data-lucide="plug" class="lucide"></i></span>
        <span class="market-offer-text">
          <span class="market-offer-title">${esc(name)}${srv?.disabled ? ` · ${t("market.disabled")}` : ""}</span>
          ${detail ? `<span class="market-offer-desc">${esc(detail)}</span>` : ""}
        </span>
      </div>`;
  }

  /**
   * Read a skill in the app's floating reader card — the same shell and rules
   * as the memory reader and the settings skill view: the card mounts on
   * `#main-content`, the document owns the whole body, and there is exactly ONE
   * exit, the header's back arrow (no footer row, so no second exit competing
   * with it).
   *
   * A skill is a tree, not a page: its `SKILL.md` cites `references/…`,
   * templates and scripts that live beside it.  The reader therefore lists the
   * package's own files under the entry document and follows both a list entry
   * and a link inside a document.  The back arrow walks up one level and closes
   * the card at the top of the trail.
   *
   * The page holds its repaints while this is open, so closing never reveals a
   * list that was rebuilt underneath the reader.
   */
  private openSkillReader(name: string): void {
    if (!this.skillDetails.get(name)?.body) return;
    this.readerView = { skill: name, trail: [""] };
    this.mountSkillReader();
    this.renderBody();
  }

  /** Put the reader card on the page once; navigation only rewrites its body. */
  private mountSkillReader(): void {
    if (this.skillReader) return;
    const host = document.getElementById("main-content");
    if (!host) return;
    const card = document.createElement("div");
    card.className = "panel-editor";
    card.dataset.panel = "market-skill";
    card.innerHTML = `
      <div class="toast-dialog dialog-wide panel-editor-dialog encre-dialog-card skill-preview-dialog">
        <div class="toast-title panel-editor-title">
          <button type="button" class="btn-icon btn-icon--sm panel-editor-back" data-action="reader-back" data-tooltip="${esc(t("sidebar.back"))}">
            <i data-lucide="arrow-left" class="lucide"></i>
          </button>
          <span class="panel-editor-title-text"></span>
        </div>
        <div class="dialog-body"></div>
      </div>`;
    host.appendChild(card);
    this.skillReader = card;
    card.addEventListener("click", (e) => this.onSkillReaderClick(e));
    // The title bar lives outside the scroll body, so its back arrow needs
    // its own pass: without this the glyph stayed an empty <i>.
    this.refreshIcons(card);
    this.renderSkillReader();
  }

  /** Draw the file the reader is on, asking the backend for it when it is not
   *  in the cache yet — the waiting state is the looping bar, not a fake fill. */
  private renderSkillReader(): void {
    const card = this.skillReader;
    const view = this.readerView;
    if (!card || !view) return;
    const path = view.trail[view.trail.length - 1];
    const title = card.querySelector(".panel-editor-title-text") as HTMLElement | null;
    if (title) title.textContent = path ? path : `Skill : ${view.skill}`;
    const body = card.querySelector(".dialog-body") as HTMLElement | null;
    if (!body) return;
    if (path) {
      const key = `${view.skill}::${path}`;
      const cached = this.skillFiles.get(key);
      if (cached !== undefined) {
        body.innerHTML = this.skillFileHtml(path, cached);
        this.refreshIcons(body);
      } else if (this.skillFileErrors.has(key)) {
        body.innerHTML = `<div class="market-file-quiet">${esc(t("market.skillFileUnreadable"))}</div>`;
      } else {
        body.innerHTML = progressBar("indeterminate");
        if (!this.skillFilePending.has(key)) {
          this.skillFilePending.add(key);
          send({ type: "get_skill_file", name: view.skill, path });
        }
      }
      this.refreshIcons(body);
      return;
    }
    const files = this.skillDetails.get(view.skill)?.files || [];
    // The list belongs INSIDE the scroll track: the card owns exactly one, and
    // a package can carry dozens of documents.
    body.innerHTML = `
      <div class="msg-text skill-preview-content">${renderMarkdown(this.skillDetails.get(view.skill)?.body || "")}
        ${files.length ? `
        <div class="market-sec-title">${esc(t("market.skillFiles"))}<span class="market-sec-count">${files.length}</span></div>
        <div class="market-file-grid">${files.map((file) => `
          <button type="button" class="market-file" data-action="skill-file" data-path="${esc(file)}">
            <i data-lucide="file-text" class="lucide"></i>${esc(file)}
          </button>`).join("")}</div>` : ""}
      </div>`;
    this.refreshIcons(body);
  }

  /** A package file as readable text: markdown for the sheets that are prose,
   *  a monospace block for everything else the skill ships — both inside the
   *  card's single scroll track. */
  private skillFileHtml(path: string, content: string): string {
    return /\.(md|markdown)$/i.test(path)
      ? `<div class="msg-text skill-preview-content">${renderMarkdown(content)}</div>`
      : `<div class="msg-text skill-preview-content"><pre class="market-file-code">${esc(content)}</pre></div>`;
  }

  /** One delegated listener for the whole card: list entries, links inside a
   *  document, and the back arrow that owns the trail. */
  private onSkillReaderClick(e: MouseEvent): void {
    const view = this.readerView;
    if (!view) return;
    const row = (e.target as HTMLElement).closest<HTMLElement>("[data-action='skill-file']");
    if (row) {
      this.enterSkillFile(row.dataset.path || "");
      return;
    }
    if ((e.target as HTMLElement).closest("[data-action='reader-back']")) {
      if (view.trail.length > 1) {
        view.trail.pop();
        this.renderSkillReader();
      } else {
        this.closeSkillReader();
      }
      return;
    }
    const md = (e.target as HTMLElement).closest(".skill-preview-content");
    if (md) {
      handleMarkdownContentClick(md as HTMLElement, e, {
        onOpenExternal: openRoutedLink,
        onOpenFile: (href) => this.followSkillLink(view, href),
      });
    }
  }

  /** Follow a link inside a skill document to another file of the same skill.
   *  Links that point at nothing the package actually carries are dropped: the
   *  reader never pretends a document exists. */
  private followSkillLink(view: { skill: string; trail: string[] }, href: string): void {
    const here = view.trail[view.trail.length - 1];
    const dir = here.includes("/") ? here.slice(0, here.lastIndexOf("/")) : "";
    const target = resolveSkillPath(dir ? `${dir}/${href}` : href);
    const files = this.skillDetails.get(view.skill)?.files || [];
    if (files.includes(target)) this.enterSkillFile(target);
  }

  /** Walk one level deeper into the skill's files. */
  private enterSkillFile(path: string): void {
    if (!this.readerView || !path) return;
    this.readerView.trail.push(path);
    this.renderSkillReader();
  }

  /** Take the reader card away and release the held repaint. */
  private closeSkillReader(): void {
    if (!this.skillReader) return;
    this.skillReader.remove();
    this.skillReader = null;
    this.readerView = null;
    this.renderBody();
  }

  // ── Events (delegated once in the constructor) ────────────────────

  private onClick(e: MouseEvent): void {
    const target = (e.target as HTMLElement).closest("[data-action]") as HTMLElement | null;
    if (!target) return;
    const name = target.dataset.name || "";
    switch (target.dataset.action) {
      case "skill":
        this.openSkillReader(target.dataset.skill || "");
        break;
      case "more":
        this.openMoreMenu(target);
        break;

      case "category": {
        const key = (target.dataset.group || "") as MarketGroup | "all";
        if (key && key !== this.category) {
          this.category = key;
          this.renderBody();
        }
        break;
      }
      case "tag": {
        const tag = target.dataset.tag || "";
        if (tag !== this.tagFilter) {
          this.tagFilter = tag;
          this.renderBody();
        }
        break;
      }
      // 已安装 management lives in settings (插件 panel): the market only
      // sells the remote catalogue, so its button hands over to that panel
      // instead of growing a second installed list with its own rules.
      case "open-installed": {
        (window as any).__encreOpenSettings?.("plugins");
        break;
      }
      case "add-plugin": {
        this.openAddPanel();
        break;
      }
      case "detail":
        this.detailName = name;
        this.renderBody();
        break;
      case "install": {
        if (!name || this.pending.has(name)) return;
        this.beginOp(name, "install");
        send({ type: "market_install", name });
        break;
      }
    }
  }

  private onInput(e: Event): void {
    const input = e.target as HTMLElement;
    if (!input.classList.contains("market-search-input")) return;
    // Debounced: every keystroke rebuilds the whole filtered grid, so a
    // fast typist must not queue dozens of full re-renders.
    if (this.searchTimer !== null) window.clearTimeout(this.searchTimer);
    this.searchTimer = window.setTimeout(() => {
      this.searchTimer = null;
      this.query = (input as HTMLInputElement).value.trim().toLowerCase();
      if (this.detailName) this.detailName = null;
      this.renderBody();
    }, 150);
  }

  private refreshIcons(root: HTMLElement): void {
    safeCreateIcons(root);
  }
}
