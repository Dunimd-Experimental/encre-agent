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
 * WITHOUT WARRANT0ES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import * as ContextMenuNs from "./context-menu.js";
import * as DialogNs from "./dialog.js";
import * as TooltipNs from "./tooltip.js";
import * as TabTooltipNs from "./tab-tooltip.js";
import * as IconsNs from "./icons.js";
import * as TransitionNs from "./transition-helper.js";
import * as SplashNs from "./splash.js";
import * as ToolBodyNs from "../chat/tool_body.js";
import * as ToolMetaNs from "../chat/tool_meta.js";
import * as PluginErrorCardNs from "../chat/plugin_error_card.js";
import * as ShortcutDisplayNs from "./shortcut-display.js";
import * as SlashCommandsNs from "./slash-commands.js";
import * as DropdownNs from "./dropdown.js";
import * as ProgressNs from "./progress.js";
import * as LogViewNs from "./log_view.js";
import * as CrumbsNs from "./crumbs.js";
import * as InputExpandNs from "./input-expand.js";
import * as ScrollbarRevealNs from "./scrollbar-reveal.js";
import * as AnimateNs from "./animate.js";
import * as InfoWidgetsNs from "./info-widgets.js";
import * as ColorPickerNs from "./color-picker.js";
import { EA_UI_CLASS_MAP } from "../plugin_sdk/contract.js";
import type { EaUiRpcName } from "../plugin_sdk/contract.js";
import type { NotificationItem, ToolCallState } from "../core/types.js";

/**
 * EA-UI alias layer — the plugin-facing name space for first-party controls.
 *
 * Plugins never reference internal class names.  They write the stable
 * `ea-ui-*` names in contributed markup (declarative `ui_override` templates,
 * sandboxed-worker `Encre.ui.render` / `Encre.ui.setSlot` HTML) and this
 * layer translates them back to the real first-party classes at the render
 * entry points.  Internal class names stay the single source of truth:
 * renaming an internal class only touches the map below, never plugin code.
 *
 * Mapping rules:
 *   - Simple aliases map one external name to one internal class
 *     (`ea-ui-card` -> `settings-card`).
 *   - Compound aliases map to a full class string, mirroring how first-party
 *     markup always combines them (`ea-ui-button-sm` -> `btn btn-sm`).  The
 *     alias replaces the whole token, so plugins never need to know about the
 *     base classes.
 *
 * Module-level controls (ContextMenu, Dialog, Dropdown, ...) are aliased in
 * `ui/components.ts` (`EA_UI_MODULES`); the CSS-class controls live here.
 */

/** Re-exported from the unified plugin contract. */
export { EA_UI_CLASS_MAP } from "../plugin_sdk/contract.js";
export type { EaUiClassAlias } from "../plugin_sdk/contract.js";

/**
 * Rewrites every `ea-ui-*` class in a plugin HTML fragment to the mapped
 * first-party classes.  Only `class` attributes are rewritten — never text
 * content, other attributes or structure.  Fragments without any alias take
 * a zero-cost fast path (no DOM parse), so plugins that do not use the
 * alias layer pay nothing.
 *
 * Unknown `ea-ui-*` names are left untouched: a typo renders unstyled
 * instead of breaking the fragment.
 */
export function translateEaUiClasses(html: string): string {
  if (!html || html.indexOf("ea-ui-") === -1) return html;
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  let changed = false;
  tpl.content.querySelectorAll<Element>("*").forEach((el) => {
    const cls = el.getAttribute("class");
    if (!cls || cls.indexOf("ea-ui-") === -1) return;
    const mapped = cls.split(/\s+/).map((c) => (EA_UI_CLASS_MAP as Record<string, string>)[c] ?? c).join(" ").trim();
    if (mapped !== cls) {
      el.setAttribute("class", mapped);
      changed = true;
    }
  });
  return changed ? tpl.innerHTML : html;
}

// ─────────────────────────────────────────────────────────────────────────────
// EA-UI RPC — the JSON-safe invokable surface of `EA_UI_MODULES`.
//
// Sandboxed plugin workers have no DOM; `Encre.eaui.call(name, ...args)`
// (see core/plugin_runtime.ts) routes a method name plus JSON args into the
// table below. Conventions at the JSON boundary:
//   - Element params are selector strings, resolved with `ctx.root.querySelector`
//     so every DOM touch stays inside the calling plugin's own container.
//   - Callback params cannot cross JSON: an entry accepts an action string and
//     the wrapper forwards the occurrence to the worker as an `eaui_event`
//     ({ source: "<rpc-name>", ...payload }).
//   - Stateful handles (progress dialogs, log views) live in a module-level
//     registry keyed `eaui-h-<n>`; the call returns `{ handle: id }` and
//     follow-up methods take the id back.
//   - Entries validate their args defensively and throw descriptive Errors —
//     a bad call must never crash the host.
// ─────────────────────────────────────────────────────────────────────────────

/** Calling context handed to every EA-UI RPC invocation. */
export interface EaUiCallContext {
  /** The calling plugin's own root container; element refs resolve inside it. */
  root: ParentNode;
  /** Post an event back to the calling worker. */
  emit: (name: string, detail: unknown) => void;
}

export type EaUiRpcFn = (args: unknown[], ctx: EaUiCallContext) => unknown;

// ── Argument helpers ─────────────────────────────────────────────────────────

function reqStr(args: unknown[], i: number, name: string): string {
  const v = args[i];
  if (typeof v !== "string") throw new Error(`eaui: '${name}' must be a string`);
  return v;
}

function optStr(args: unknown[], i: number): string | undefined {
  const v = args[i];
  return typeof v === "string" ? v : undefined;
}

function reqNum(args: unknown[], i: number, name: string): number {
  const v = args[i];
  if (typeof v !== "number" || !Number.isFinite(v)) throw new Error(`eaui: '${name}' must be a number`);
  return v;
}

function optNum(args: unknown[], i: number): number | undefined {
  const v = args[i];
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function reqArr(args: unknown[], i: number, name: string): unknown[] {
  const v = args[i];
  if (!Array.isArray(v)) throw new Error(`eaui: '${name}' must be an array`);
  return v;
}

/** Resolve a selector inside the calling plugin's own root container. */
function reqEl(ctx: EaUiCallContext, sel: unknown): HTMLElement {
  if (typeof sel !== "string" || !sel) throw new Error("eaui: element selector required");
  const el = ctx.root.querySelector(sel);
  if (!(el instanceof HTMLElement)) throw new Error(`eaui: element not found: ${sel}`);
  return el;
}

/** Forward one callback occurrence to the calling worker. */
function emitEvent(ctx: EaUiCallContext, source: string, payload: Record<string, unknown>): void {
  ctx.emit("eaui_event", { source, ...payload });
}

// ── Stateful handle registry ─────────────────────────────────────────────────

let _handleSeq = 0;
const _handles = new Map<string, unknown>();

function putHandle(value: unknown): string {
  const id = `eaui-h-${++_handleSeq}`;
  _handles.set(id, value);
  return id;
}

function takeHandle(id: unknown): unknown {
  if (typeof id !== "string" || !id) throw new Error("eaui: handle id required");
  const value = _handles.get(id);
  if (value === undefined) throw new Error(`eaui: unknown handle '${id}'`);
  return value;
}

// ── Host app access ──────────────────────────────────────────────────────────

/** The host App instance (stashed by app.ts); throws when not ready yet. */
function reqApp(): any {
  const app = (window as any).__app;
  if (!app) throw new Error("eaui: host app not ready");
  return app;
}

// ── JSON -> model converters ─────────────────────────────────────────────────

/** Strip non-JSON-safe fields (onClick) from a context-menu row spec. */
function toMenuItemSpec(raw: unknown): ContextMenuNs.ContextMenuItemSpec {
  if (typeof raw !== "object" || raw === null) throw new Error("eaui: context-menu item must be an object");
  const o = raw as Record<string, unknown>;
  const spec: ContextMenuNs.ContextMenuItemSpec = {};
  if (o.divider === true) spec.divider = true;
  if (typeof o.id === "string") spec.id = o.id;
  if (typeof o.action === "string") spec.action = o.action;
  if (typeof o.label === "string") spec.label = o.label;
  if (typeof o.icon === "string") spec.icon = o.icon;
  if (o.danger === true) spec.danger = true;
  if (o.disabled === true) spec.disabled = true;
  if (typeof o.title === "string") spec.title = o.title;
  if (typeof o.className === "string") spec.className = o.className;
  if (typeof o.data === "object" && o.data !== null) {
    const data: Record<string, string> = {};
    for (const [k, v] of Object.entries(o.data)) {
      if (typeof v === "string") data[k] = v;
    }
    spec.data = data;
  }
  return spec;
}

function toTooltipModel(raw: unknown): TooltipNs.TooltipModel | null {
  if (raw === null) return null;
  if (typeof raw !== "object" || raw === null) throw new Error("eaui: tooltip model must be an object or null");
  const o = raw as Record<string, unknown>;
  if (typeof o.title !== "string") throw new Error("eaui: tooltip model 'title' must be a string");
  const rows: TooltipNs.TooltipRow[] = [];
  if (o.rows !== undefined && !Array.isArray(o.rows)) throw new Error("eaui: tooltip model 'rows' must be an array");
  for (const r of (o.rows ?? []) as unknown[]) {
    if (typeof r !== "object" || r === null) throw new Error("eaui: tooltip row must be an object");
    const ro = r as Record<string, unknown>;
    if (typeof ro.value !== "string") throw new Error("eaui: tooltip row 'value' must be a string");
    rows.push({ label: typeof ro.label === "string" ? ro.label : undefined, value: ro.value });
  }
  const perms: TooltipNs.TooltipPermission[] = [];
  if (o.permissions !== undefined && !Array.isArray(o.permissions)) {
    throw new Error("eaui: tooltip model 'permissions' must be an array");
  }
  for (const p of (o.permissions ?? []) as unknown[]) {
    if (typeof p !== "object" || p === null) throw new Error("eaui: tooltip permission must be an object");
    const po = p as Record<string, unknown>;
    if (typeof po.icon !== "string" || typeof po.label !== "string") {
      throw new Error("eaui: tooltip permission needs 'icon' and 'label' strings");
    }
    perms.push({ icon: po.icon, label: po.label, granted: po.granted !== false });
  }
  return {
    title: o.title,
    icon: typeof o.icon === "string" ? o.icon : undefined,
    rows,
    permissions: perms.length ? perms : undefined,
  };
}

function toTabTooltipData(raw: unknown): TabTooltipNs.TabTooltipData {
  if (typeof raw !== "object" || raw === null) throw new Error("eaui: tab tooltip data must be an object");
  const o = raw as Record<string, unknown>;
  const data: TabTooltipNs.TabTooltipData = { title: typeof o.title === "string" ? o.title : "" };
  if (typeof o.icon === "string") data.icon = o.icon;
  if (typeof o.location === "string") data.location = o.location;
  if (o.isUrl === true) data.isUrl = true;
  if (typeof o.size === "number" && Number.isFinite(o.size)) data.size = o.size;
  if (typeof o.mtime === "number" && Number.isFinite(o.mtime)) data.mtime = o.mtime;
  if (Array.isArray(o.permissions)) {
    const perms: Array<{ name: string; granted: boolean }> = [];
    for (const p of o.permissions) {
      if (typeof p !== "object" || p === null) continue;
      const po = p as Record<string, unknown>;
      if (typeof po.name !== "string") continue;
      perms.push({ name: po.name, granted: po.granted !== false });
    }
    data.permissions = perms;
  }
  return data;
}

function toDropdownOptions(raw: unknown): DropdownNs.DropdownOption[] {
  if (!Array.isArray(raw)) throw new Error("eaui: dropdown options must be an array");
  const out: DropdownNs.DropdownOption[] = [];
  for (const o of raw) {
    if (typeof o !== "object" || o === null) throw new Error("eaui: dropdown option must be an object");
    const oo = o as Record<string, unknown>;
    if (typeof oo.id !== "string" || typeof oo.label !== "string") {
      throw new Error("eaui: dropdown option needs 'id' and 'label' strings");
    }
    out.push({ id: oo.id, label: oo.label, icon: typeof oo.icon === "string" ? oo.icon : undefined });
  }
  return out;
}

/** Build a `NotificationItem` from a JSON payload; unknown fields are dropped. */
function toNotificationItem(raw: unknown): NotificationItem {
  if (typeof raw !== "object" || raw === null) throw new Error("eaui: notification must be an object");
  const o = raw as Record<string, unknown>;
  if (typeof o.id !== "string") throw new Error("eaui: notification 'id' must be a string");
  if (o.type !== "error" && o.type !== "success" && o.type !== "info" && o.type !== "warning") {
    throw new Error("eaui: notification 'type' must be 'error' | 'success' | 'info' | 'warning'");
  }
  if (typeof o.title !== "string") throw new Error("eaui: notification 'title' must be a string");
  if (typeof o.message !== "string") throw new Error("eaui: notification 'message' must be a string");
  const item: NotificationItem = {
    id: o.id,
    type: o.type,
    title: o.title,
    message: o.message,
    timestamp: typeof o.timestamp === "number" && Number.isFinite(o.timestamp) ? o.timestamp : Date.now(),
    read: o.read === true,
  };
  if (typeof o.source === "string") item.source = o.source;
  if (typeof o.media === "object" && o.media !== null) {
    const m = o.media as Record<string, unknown>;
    if ((m.type === "image" || m.type === "video") && typeof m.src === "string") {
      item.media = { type: m.type, src: m.src };
    }
  }
  return item;
}

/**
 * JSON cannot carry functions: a crumb level's `action` string becomes the
 * click callback, announced to the worker as an `eaui_event`.
 */
function toCrumbLevels(raw: unknown, ctx: EaUiCallContext, rpc: string): CrumbsNs.CrumbLevel[] {
  if (!Array.isArray(raw)) throw new Error("eaui: crumbs levels must be an array");
  return raw.map((lvl, i) => {
    if (typeof lvl !== "object" || lvl === null) throw new Error("eaui: crumb level must be an object");
    const lo = lvl as Record<string, unknown>;
    if (typeof lo.label !== "string") throw new Error("eaui: crumb level 'label' must be a string");
    const action = typeof lo.action === "string" ? lo.action : "";
    return {
      label: lo.label,
      onActivate: action ? () => emitEvent(ctx, rpc, { action, index: i }) : undefined,
    };
  });
}

/** Build the minimal `ToolCallState` the fault card actually reads. */
function toErrorContext(raw: unknown): PluginErrorCardNs.PluginErrorContext {
  if (typeof raw !== "object" || raw === null) throw new Error("eaui: error-card context must be an object");
  const o = raw as Record<string, unknown>;
  if (typeof o.plugin !== "string") throw new Error("eaui: error-card context needs a 'plugin' string");
  const t = (typeof o.tc === "object" && o.tc !== null ? o.tc : {}) as Record<string, unknown>;
  const tc: ToolCallState = {
    id: typeof t.id === "string" ? t.id : "eaui-tc",
    name: typeof t.name === "string" ? t.name : "plugin",
    params: {},
    status: "done",
    result: typeof t.result === "string" ? t.result : undefined,
    errorAt: typeof t.errorAt === "number" ? t.errorAt : undefined,
  };
  return { tc, plugin: o.plugin, expanded: o.expanded === true };
}

// ── The RPC table ────────────────────────────────────────────────────────────

/** A fresh SplashScreen instance is safe here: `hide()` only toggles the
 *  splash DOM, and this instance never showed anything (no loader to destroy).
 *  The app's own instance is not reachable from the renderer bundle. */
let _splash: SplashNs.SplashScreen | null = null;

/**
 * JSON-safe invokable surface for EA_UI_MODULES, keyed `"<ea-ui-name>.<method>"`.
 * Consumed by the plugin-worker bridge (`Encre.eaui.call` in
 * core/plugin_runtime.ts); the method list is injected into each worker at
 * spawn so it can validate names offline.
 */
export const EA_UI_RPC: Record<string, EaUiRpcFn> = {
  // ── ea-ui-context-menu ──────────────────────────────────────────────────
  "context-menu.show": (args, ctx) => {
    ContextMenuNs.showContextMenu(reqEl(ctx, args[0]), reqNum(args, 1, "x"), reqNum(args, 2, "y"));
  },
  "context-menu.close": (args, ctx) => {
    ContextMenuNs.closeContextMenu(reqEl(ctx, args[0]));
  },
  "context-menu.render-items": (args, ctx) => {
    const menu = reqEl(ctx, args[0]);
    const items = reqArr(args, 1, "items").map(toMenuItemSpec);
    ContextMenuNs.renderContextMenuItems(items, {
      container: menu,
      onAction: (spec) =>
        emitEvent(ctx, "context-menu.render-items", {
          action: spec.action ?? spec.id ?? "",
          data: spec.data ?? {},
        }),
    });
  },
  "context-menu.item-html": (args) => ContextMenuNs.contextMenuItemHtml(toMenuItemSpec(args[0])),

  // ── ea-ui-dialog ────────────────────────────────────────────────────────
  "dialog.confirm": async (args) =>
    await DialogNs.Dialog.confirm(reqStr(args, 0, "title"), reqStr(args, 1, "message")),
  "dialog.prompt": async (args) =>
    await DialogNs.Dialog.prompt(reqStr(args, 0, "title"), reqStr(args, 1, "message")),
  "dialog.alert": async (args) => {
    await DialogNs.Dialog.alert(reqStr(args, 0, "title"), reqStr(args, 1, "message"));
    return null;
  },
  "dialog.progress.open": (args) => {
    const handle: DialogNs.ProgressHandle = DialogNs.Dialog.progress(
      reqStr(args, 0, "title"),
      reqStr(args, 1, "message"),
    );
    return { handle: putHandle(handle) };
  },
  "dialog.progress.update": (args) => {
    (takeHandle(args[0]) as DialogNs.ProgressHandle).update(reqNum(args, 1, "progress"), optStr(args, 2));
  },
  "dialog.progress.set-message": (args) => {
    (takeHandle(args[0]) as DialogNs.ProgressHandle).setMessage(reqStr(args, 1, "message"));
  },
  "dialog.progress.set-sub-message": (args) => {
    (takeHandle(args[0]) as DialogNs.ProgressHandle).setSubMessage(reqStr(args, 1, "message"));
  },
  "dialog.progress.succeed": (args) => {
    (takeHandle(args[0]) as DialogNs.ProgressHandle).succeed(reqStr(args, 1, "message"));
  },
  "dialog.progress.fail": (args) => {
    (takeHandle(args[0]) as DialogNs.ProgressHandle).fail(reqStr(args, 1, "message"));
  },
  "dialog.progress.cancel": (args) => {
    (takeHandle(args[0]) as DialogNs.ProgressHandle).cancel();
  },

  // ── ea-ui-tooltip ───────────────────────────────────────────────────────
  "tooltip.set-model": (args, ctx) => {
    TooltipNs.setTooltipModel(reqEl(ctx, args[0]), toTooltipModel(args[1]));
  },
  "tooltip.show-at": (args) => {
    TooltipNs.showTooltipAt(reqStr(args, 0, "text"), reqNum(args, 1, "x"), reqNum(args, 2, "y"));
  },
  "tooltip.hide": () => TooltipNs.hideTooltip(),

  // ── ea-ui-tab-tooltip ───────────────────────────────────────────────────
  "tab-tooltip.format-file-size": (args) => TabTooltipNs.formatFileSize(reqNum(args, 0, "bytes")),
  "tab-tooltip.format-timestamp": (args) => TabTooltipNs.formatTimestamp(reqNum(args, 0, "ms"), optStr(args, 1)),
  "tab-tooltip.build": (args) => TabTooltipNs.buildTabTooltip(toTabTooltipData(args[0]), optStr(args, 1)),
  "tab-tooltip.apply": (args, ctx) => {
    TabTooltipNs.applyTabTooltip(reqEl(ctx, args[0]), toTabTooltipData(args[1]), optStr(args, 2));
  },

  // ── ea-ui-toast ─────────────────────────────────────────────────────────
  // Routes through the host App instance (stashed on `window.__app` by app.ts).
  "toast.show": (args) => {
    reqApp().notifications.showToast(toNotificationItem(args[0]));
  },

  // ── ea-ui-permission ────────────────────────────────────────────────────
  // Real signature: `show(toolName, reason, cb, container?)`. The decision
  // callback cannot cross JSON, so its slot is reserved (index 2) and the
  // outcome is announced to the worker as an `eaui_event`; the optional
  // container stays positional at index 3 as a selector string.
  "permission.show": (args, ctx) => {
    const container = typeof args[3] === "string" && args[3] ? reqEl(ctx, args[3]) : undefined;
    reqApp().permissions.show(
      reqStr(args, 0, "toolName"),
      reqStr(args, 1, "reason"),
      (decision: "allow" | "allow_session" | "deny") => emitEvent(ctx, "permission.show", { decision }),
      container,
    );
  },

  // ── ea-ui-icon ──────────────────────────────────────────────────────────
  "icon.platform-html": (args) =>
    IconsNs.platformIconHtml(reqStr(args, 0, "id"), optNum(args, 1) ?? 22, optStr(args, 2) ?? "", optStr(args, 3) ?? ""),
  "icon.platform-src": (args) => IconsNs.platformIconSrc(reqStr(args, 0, "id")),
  "icon.search-engine-src": (args) => IconsNs.searchEngineIconSrc(reqStr(args, 0, "id")),
  "icon.asset-html": (args) =>
    IconsNs.assetIconHtml(reqStr(args, 0, "src"), optNum(args, 1) ?? 22, optStr(args, 2) ?? "", optStr(args, 3) ?? ""),

  // ── ea-ui-view ──────────────────────────────────────────────────────────
  // Routes through the host App instance; `switchTo` silently ignores an
  // unregistered view id (the manager's own guard).
  "view.switch-to": (args) => {
    reqApp().viewManager.switchTo(reqStr(args, 0, "view"));
  },
  "view.current": () => reqApp().viewManager.getCurrentView(),

  // ── ea-ui-transition ────────────────────────────────────────────────────
  "transition.step": (args) =>
    TransitionNs.TransitionHelper.step(reqStr(args, 0, "token"), reqNum(args, 1, "fallback")),
  "transition.default-duration": () => TransitionNs.TransitionHelper.DEFAULT_DURATION,

  // ── ea-ui-splash ────────────────────────────────────────────────────────
  "splash.hide": () => {
    if (!_splash) _splash = new SplashNs.SplashScreen();
    _splash.hide();
  },

  // ── ea-ui-tool-body ─────────────────────────────────────────────────────
  "tool-body.diff": (args) => ToolBodyNs.renderDiffBody(reqStr(args, 0, "result")),
  "tool-body.terminal": (args) => ToolBodyNs.renderTerminalBody(reqStr(args, 0, "result")),
  "tool-body.web-results": (args) => ToolBodyNs.renderWebResults(reqStr(args, 0, "result")),
  "tool-body.web-fetched": (args) => ToolBodyNs.renderWebFetchedContent(reqStr(args, 0, "result")),
  "tool-body.pre": (args) => ToolBodyNs.renderPreBody(reqStr(args, 0, "result")),

  // ── ea-ui-tool-meta ─────────────────────────────────────────────────────
  "tool-meta.for": (args) => ToolMetaNs.metaFor(reqStr(args, 0, "name")),
  "tool-meta.label": (args) => ToolMetaNs.labelFor(reqStr(args, 0, "name"), optStr(args, 1)),
  "tool-meta.icon": (args) => ToolMetaNs.iconFor(reqStr(args, 0, "name")),
  "tool-meta.source": (args) => ToolMetaNs.toolSource(reqStr(args, 0, "name")),
  "tool-meta.is-plugin": (args) => ToolMetaNs.isPluginTool(reqStr(args, 0, "name")),
  "tool-meta.is-hidden": (args) => ToolMetaNs.isToolHidden(reqStr(args, 0, "name")),
  "tool-meta.count": () => ToolMetaNs.toolMetaCount(),

  // ── ea-ui-shortcut ──────────────────────────────────────────────────────
  "shortcut.format": (args) => ShortcutDisplayNs.formatShortcut(reqStr(args, 0, "pattern")),
  "shortcut.lookup": (args) => ShortcutDisplayNs.lookupShortcut(reqStr(args, 0, "id")),
  "shortcut.augment-title": (args) =>
    ShortcutDisplayNs.augmentTitle(reqStr(args, 0, "existing"), reqStr(args, 1, "shortcut")),
  "shortcut.platform-label": () => ShortcutDisplayNs.platformLabel(),

  // ── ea-ui-slash-commands ────────────────────────────────────────────────
  "slash-commands.find": (args) => SlashCommandsNs.findSlashCommand(reqStr(args, 0, "name")) ?? null,
  "slash-commands.parse": (args) => SlashCommandsNs.parseSlashInput(reqStr(args, 0, "text")) ?? null,
  "slash-commands.matching": (args) =>
    SlashCommandsNs.matchingSlashCommands(reqStr(args, 0, "query"), optStr(args, 1)),

  // ── ea-ui-error-card ────────────────────────────────────────────────────
  "error-card.render": (args) => PluginErrorCardNs.renderPluginError(toErrorContext(args[0])),

  // ── ea-ui-select ────────────────────────────────────────────────────────
  // `renderDropdownImpl`'s `this` is spurious (the body never uses it) — the
  // first-party convention is `.call(null, ...)`; the change callback is
  // dropped here because the markup is bound afterwards via `select.bind`.
  "select.render": (args) =>
    DropdownNs.renderDropdownImpl.call(null, reqStr(args, 0, "id"), toDropdownOptions(args[1]), optStr(args, 2) ?? "", () => {}),
  "select.bind": (args, ctx) => {
    const id = reqStr(args, 0, "id");
    DropdownNs.bindDropdownImpl.call(null, id, (val: string) => emitEvent(ctx, "select.bind", { id, value: val }));
  },

  // ── ea-ui-progress ──────────────────────────────────────────────────────
  "progress.bar": (args) => {
    const kind = reqStr(args, 0, "kind");
    if (kind !== "indeterminate" && kind !== "completing") {
      throw new Error("eaui: progress 'kind' must be 'indeterminate' or 'completing'");
    }
    return ProgressNs.progressBar(kind);
  },

  // ── ea-ui-log-view ──────────────────────────────────────────────────────
  // The real `LogViewHandle` only carries `destroy()` — the view loads and
  // refreshes its rows itself (main-process log polling), so there is no
  // append/clear surface to expose.
  "log-view.mount": (args, ctx) => {
    const handle: LogViewNs.LogViewHandle = LogViewNs.mountLogView(reqEl(ctx, args[0]));
    return { handle: putHandle(handle) };
  },
  "log-view.destroy": (args) => {
    (takeHandle(args[0]) as LogViewNs.LogViewHandle).destroy();
  },

  // ── ea-ui-breadcrumbs ───────────────────────────────────────────────────
  "crumbs.render": (args, ctx) => {
    CrumbsNs.renderCrumbs(reqEl(ctx, args[0]), toCrumbLevels(args[1], ctx, "crumbs.render"));
  },

  // ── ea-ui-input-expand ──────────────────────────────────────────────────
  // No selector -> attach to the calling plugin's whole root. Idempotent per
  // container, so re-rendering plugin markup never duplicates the toggle.
  "input-expand.attach": (args, ctx) => {
    InputExpandNs.attachInputExpand(typeof args[0] === "string" && args[0] ? reqEl(ctx, args[0]) : ctx.root);
  },

  // ── ea-ui-scrollbar ─────────────────────────────────────────────────────
  // Installs one app-wide listener; the module-level guard makes re-runs a
  // no-op, so any plugin may call it safely.
  "scrollbar.init": () => ScrollbarRevealNs.initScrollbarReveal(),

  // ── ea-ui-animate ───────────────────────────────────────────────────────
  "animate.show": (args, ctx) => {
    AnimateNs.showAnimated(reqEl(ctx, args[0]));
  },
  "animate.hide": (args, ctx) => {
    AnimateNs.hideAnimated(reqEl(ctx, args[0]));
  },
  "animate.remove": (args, ctx) => {
    AnimateNs.removeAnimated(reqEl(ctx, args[0]), () => emitEvent(ctx, "animate.remove", { el: args[0] }));
  },
  "animate.show-view": (args, ctx) => {
    AnimateNs.showViewAnimated(reqEl(ctx, args[0]), args[1] !== false);
  },
  "animate.hide-view": (args, ctx) => {
    AnimateNs.hideViewAnimated(reqEl(ctx, args[0]));
  },

  // ── ea-ui-info-widget ───────────────────────────────────────────────────
  // Pure HTML-string factories (like context-menu.item-html): the worker
  // embeds the returned fragment into its own markup and re-renders on state
  // changes.  Args carry the spec object directly; the factories validate.
  "info-widget.chip": (args) => InfoWidgetsNs.infoChipHtml(args[0] as InfoWidgetsNs.InfoChipSpec),
  "info-widget.card": (args) => InfoWidgetsNs.infoCardHtml(args[0] as InfoWidgetsNs.InfoCardSpec),

  // ── ea-ui-color-picker ──────────────────────────────────────────────────
  // Same two-step contract as select.render/bind: render returns the unified
  // markup, bind wires it and reports picks as an `eaui_event`.
  "color-picker.render": (args) =>
    ColorPickerNs.renderColorPickerImpl.call(null, reqStr(args, 0, "id"), optStr(args, 1) ?? ColorPickerNs.DEFAULT_COLOR, () => {}),
  "color-picker.bind": (args, ctx) => {
    const id = reqStr(args, 0, "id");
    ColorPickerNs.bindColorPickerImpl.call(null, id, (hex: string) => emitEvent(ctx, "color-picker.bind", { id, value: hex }));
  },
};

// Compile-time completeness check against the unified contract: every
// advertised RPC name must exist here, and the compiler names the missing key.
const _eaUiRpcContract: Record<EaUiRpcName, EaUiRpcFn> = EA_UI_RPC;
void _eaUiRpcContract;

/**
 * Dispatch one EA-UI RPC call. Throws for unknown methods; entries may be
 * async and their promises are awaited here.
 */
export async function resolveEaUiCall(name: string, args: unknown[], ctx: EaUiCallContext): Promise<unknown> {
  if (!Object.prototype.hasOwnProperty.call(EA_UI_RPC, name)) throw new Error(`eaui: unknown method '${name}'`);
  return await EA_UI_RPC[name](args, ctx);
}
