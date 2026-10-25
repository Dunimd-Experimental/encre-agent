// ─────────────────────────────────────────────────────────────────────────────
// UI Component Barrel — the single public surface for reusable UI primitives.
//
// ES modules have no "class-level export" keyword, but a barrel file plays the
// same role as Python's `__init__.py`: it re-exports every primitive so any
// feature module imports one path instead of reaching into `ui/*` directly.
// Plugin contributions that want to reuse a primitive resolve against the
// same names here.  Plugins themselves never import this barrel: they use the
// `ea-ui-*` aliases — `EaUi`/`EA_UI_MODULES` below are the registry.
//
// `export * as X` gives each module a single import point (namespaced access),
// while the `ui` object below gives one stable handle for consumers that want
// to enumerate every module at runtime.
// ─────────────────────────────────────────────────────────────────────────────

export * as ContextMenu from "./context-menu.js";
export * as Notifications from "./notifications.js";
export * as Permissions from "./permissions.js";
export * as Dialog from "./dialog.js";
export * as Tooltip from "./tooltip.js";
export * as TabTooltip from "./tab-tooltip.js";
export * as Icons from "./icons.js";
export * as ViewManager from "./viewmanager.js";
export * as Transition from "./transition-helper.js";
export * as Splash from "./splash.js";
export * as ToolBody from "../chat/tool_body.js";
export * as ToolMeta from "../chat/tool_meta.js";
export * as ShortcutDisplay from "./shortcut-display.js";
export * as SlashCommands from "./slash-commands.js";
export * as PluginErrorCard from "../chat/plugin_error_card.js";
export * as Dropdown from "./dropdown.js";
export * as Progress from "./progress.js";
export * as LogView from "./log_view.js";
export * as Crumbs from "./crumbs.js";
export * as InputExpand from "./input-expand.js";
export * as ScrollbarReveal from "./scrollbar-reveal.js";
export * as Animate from "./animate.js";
export * as InfoWidgets from "./info-widgets.js";
// Plugin-facing alias layer: `ea-ui-*` class map + HTML translator.
export * as EaUi from "./ea_ui.js";

import type { EaUiModuleName } from "../plugin_sdk/contract.js";

import * as ContextMenu_ from "./context-menu.js";import * as Notifications_ from "./notifications.js";
import * as Permissions_ from "./permissions.js";
import * as Dialog_ from "./dialog.js";
import * as Tooltip_ from "./tooltip.js";
import * as TabTooltip_ from "./tab-tooltip.js";
import * as Icons_ from "./icons.js";
import * as ViewManager_ from "./viewmanager.js";
import * as Transition_ from "./transition-helper.js";
import * as Splash_ from "./splash.js";
import * as ToolBody_ from "../chat/tool_body.js";
import * as ToolMeta_ from "../chat/tool_meta.js";
import * as PluginErrorCard_ from "../chat/plugin_error_card.js";
import * as ShortcutDisplay_ from "./shortcut-display.js";
import * as SlashCommands_ from "./slash-commands.js";
import * as Dropdown_ from "./dropdown.js";
import * as Progress_ from "./progress.js";
import * as LogView_ from "./log_view.js";
import * as Crumbs_ from "./crumbs.js";
import * as InputExpand_ from "./input-expand.js";
import * as ScrollbarReveal_ from "./scrollbar-reveal.js";
import * as Animate_ from "./animate.js";
import * as InfoWidgets_ from "./info-widgets.js";
import * as EaUi_ from "./ea_ui.js";

export const ui = {
  ContextMenu: ContextMenu_,
  Notifications: Notifications_,
  Permissions: Permissions_,
  Dialog: Dialog_,
  Tooltip: Tooltip_,
  TabTooltip: TabTooltip_,
  Icons: Icons_,
  ViewManager: ViewManager_,
  Transition: Transition_,
  Splash: Splash_,
  ToolBody: ToolBody_,
  ToolMeta: ToolMeta_,
  PluginErrorCard: PluginErrorCard_,
  ShortcutDisplay: ShortcutDisplay_,
  SlashCommands: SlashCommands_,
  Dropdown: Dropdown_,
  Progress: Progress_,
  LogView: LogView_,
  Crumbs: Crumbs_,
  InputExpand: InputExpand_,
  ScrollbarReveal: ScrollbarReveal_,
  Animate: Animate_,
  EaUi: EaUi_,
} as const;

export type UiModules = typeof ui;

/**
 * Module-level EA-UI aliases: external plugin-facing name -> the same module
 * namespace the first-party surfaces import.  Complements the CSS-class map
 * in `ea_ui.ts` — together they are the complete `ea-ui-*` registry plugins
 * resolve against.  The map is data, so a future runtime (sandboxed workers,
 * iframe providers) can expose it without new plumbing.
 */
export const EA_UI_MODULES = {
  "ea-ui-context-menu": ContextMenu_,
  "ea-ui-dialog": Dialog_,
  "ea-ui-tooltip": Tooltip_,
  "ea-ui-tab-tooltip": TabTooltip_,
  "ea-ui-toast": Notifications_,
  "ea-ui-permission": Permissions_,
  "ea-ui-icon": Icons_,
  "ea-ui-view": ViewManager_,
  "ea-ui-transition": Transition_,
  "ea-ui-splash": Splash_,
  "ea-ui-tool-body": ToolBody_,
  "ea-ui-tool-meta": ToolMeta_,
  "ea-ui-shortcut": ShortcutDisplay_,
  "ea-ui-slash-commands": SlashCommands_,
  "ea-ui-error-card": PluginErrorCard_,
  "ea-ui-select": Dropdown_,
  "ea-ui-progress": Progress_,
  "ea-ui-log-view": LogView_,
  "ea-ui-breadcrumbs": Crumbs_,
  "ea-ui-input-expand": InputExpand_,
  "ea-ui-scrollbar": ScrollbarReveal_,
  "ea-ui-animate": Animate_,
  "ea-ui-info-widget": InfoWidgets_,
} as const satisfies Record<EaUiModuleName, unknown>;
