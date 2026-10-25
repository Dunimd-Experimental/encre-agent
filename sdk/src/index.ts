/**
 * Encre Plugin UI SDK — one entry point for every plugin-facing surface.
 *
 * It merges the four things plugins previously had to discover separately:
 *   - the `ea-ui-*` control classes (EA_UI_CLASS_MAP),
 *   - module-level components (EA_UI_MODULE_NAMES),
 *   - the JSON-safe RPC methods (EA_UI_RPC_NAMES),
 *   - slots / declarative contributions (PLUGIN_SLOTS, builders below).
 *
 * The same module runs in both supported plugin frontends:
 *   - worker modules injected with `globalThis.Encre`;
 *   - iframe providers injected with `window.encre`.
 */

import {
  EA_UI_CLASS_MAP,
  EA_UI_MODULE_NAMES,
  EA_UI_RPC_NAMES,
  EA_UI_IFRAME_RPC_ALLOWLIST,
  PLUGIN_SLOTS,
  PLUGIN_CONTRIBUTION_KINDS,
  PLUGIN_SDK_VERSION,
  PLUGIN_UI_CONTRACT_VERSION,
  type EaUiClassAlias,
  type EaUiModuleName,
  type EaUiRpcName,
  type PluginSlotId,
  type PluginContributionKind,
} from "./contract.js";

export {
  EA_UI_CLASS_MAP,
  EA_UI_MODULE_NAMES,
  EA_UI_RPC_NAMES,
  EA_UI_IFRAME_RPC_ALLOWLIST,
  PLUGIN_SLOTS,
  PLUGIN_CONTRIBUTION_KINDS,
  PLUGIN_SDK_VERSION,
  PLUGIN_UI_CONTRACT_VERSION,
};
export type { EaUiClassAlias, EaUiModuleName, EaUiRpcName, PluginSlotId, PluginContributionKind };

export interface PluginWorkerHost {
  /** Injected by the worker preamble; authorises actions.send. */
  plugin?: string;
  ui?: {
    render?: (html: string) => unknown;
    setSlot?: (slot: string, html: string) => unknown;
  };
  eaui?: { call?: (name: string, ...args: unknown[]) => Promise<unknown> };
  actions?: { send?: (kind: string, plugin: string, action: string, payload?: unknown) => unknown };
  on?: (event: string, cb: (detail: unknown) => void) => void;
}

export interface PluginIframeHost {
  base?: string;
  provider?: Record<string, unknown>;
  post?: (type: string, payload?: unknown) => void;
  call?: (name: string, ...args: unknown[]) => Promise<unknown>;
  on?: (event: string, cb: (detail: unknown) => void) => void;
}

export interface PluginUiDefinition {
  plugin?: string;
  /** Worker modules only: replace the plugin's own root with this markup. */
  render?: () => string;
  /** Named slot overrides: slot id -> markup producer. */
  slots?: Partial<Record<PluginSlotId, () => string>>;
  /** Backend `plugin_ui_result` / EA-UI callback sink. */
  onAction?: (event: { kind: string; action?: string; data?: unknown; ok?: boolean }) => void;
}

export interface PluginSettingsRow {
  id: string;
  type: "toggle" | "select" | "text" | "number" | "textarea" | "color" | "button";
  label: string;
  desc?: string;
  key?: string;
  options?: { id: string; label: string }[];
  default?: string | boolean;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  rows?: number;
  action?: string;
  button_label?: string;
}

export interface PluginContribution<T = Record<string, unknown>> {
  kind: PluginContributionKind;
  spec: T;
}

const MAX_HTML = 256 * 1024;
const IFRAME_CALL_TIMEOUT_MS = 15_000;
let _seq = 0;
const _pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: number }>();
let _bridgeInstalled = false;
const _iframeListeners = new Map<string, Set<(detail: unknown) => void>>();

function globalObject(): Record<string, unknown> {
  return globalThis as unknown as Record<string, unknown>;
}

export function workerHost(): PluginWorkerHost | null {
  const g = globalObject();
  const host = g.Encre as PluginWorkerHost | undefined;
  return host && typeof host === "object" ? host : null;
}

export function iframeHost(): PluginIframeHost | null {
  const g = globalObject() as { window?: { encre?: PluginIframeHost } };
  const host = g.window?.encre;
  return host && typeof host === "object" ? host : null;
}

export function host(): PluginWorkerHost | PluginIframeHost | null {
  return workerHost() || iframeHost();
}

export function isWorkerRuntime(): boolean {
  return workerHost() !== null;
}

export function isIframeRuntime(): boolean {
  return workerHost() === null && iframeHost() !== null;
}

function requireHost(): PluginWorkerHost | PluginIframeHost {
  const h = host();
  if (!h) {
    throw new Error(
      "Encre plugin SDK: no host bridge found. Worker modules get `Encre`; iframe providers get `window.encre`.",
    );
  }
  return h;
}

function checkHtml(html: unknown): string {
  if (typeof html !== "string" || !html) throw new Error("Encre plugin SDK: html must be a non-empty string");
  if (html.length > MAX_HTML) throw new Error(`Encre plugin SDK: html exceeds ${MAX_HTML} bytes`);
  return html;
}

function installIframeBridge(): PluginIframeHost | null {
  const h = iframeHost();
  if (!h || _bridgeInstalled) return h;
  _bridgeInstalled = true;
  const g = globalObject() as { window?: Window };
  g.window?.addEventListener("message", (event: MessageEvent) => {
    const data = event.data as {
      source?: string;
      type?: string;
      id?: number;
      ok?: boolean;
      value?: unknown;
      error?: string;
      name?: string;
      detail?: unknown;
    } | null;
    if (!data || data.source !== "encre-plugin-host") return;
    if (data.type === "eaui.result" && typeof data.id === "number") {
      const pending = _pending.get(data.id);
      if (!pending) return;
      _pending.delete(data.id);
      clearTimeout(pending.timer);
      if (data.ok) pending.resolve(data.value);
      else pending.reject(new Error(data.error || "EA-UI call failed"));
      return;
    }
    if (data.type === "event" && typeof data.name === "string") {
      const listeners = _iframeListeners.get(data.name);
      if (listeners) {
        for (const fn of Array.from(listeners)) {
          try {
            fn(data.detail);
          } catch {
            // A faulty listener must not break the bridge.
          }
        }
      }
    }
  });
  return h;
}

/** Replace the plugin's own worker root with `html`. */
export function render(html: string): void {
  const safe = checkHtml(html);
  const h = workerHost();
  if (!h?.ui?.render) {
    throw new Error("Encre plugin SDK: render() is only available in a worker module; iframe UIs own their DOM");
  }
  h.ui.render(safe);
}

/** Take over one first-party slot with `html`. */
export function setSlot(slot: string, html: string): void {
  if (!slot) throw new Error("Encre plugin SDK: slot id is required");
  const safe = checkHtml(html);
  const worker = workerHost();
  if (worker?.ui?.setSlot) {
    worker.ui.setSlot(slot, safe);
    return;
  }
  const frame = installIframeBridge();
  if (!frame?.post) throw new Error("Encre plugin SDK: setSlot() needs a worker or iframe bridge");
  frame.post("ui.setSlot", { slot, html: safe });
}

/** Invoke an EA-UI method by its stable contract name. */
export async function call<T = unknown>(name: EaUiRpcName, ...args: unknown[]): Promise<T> {
  if (!name) throw new Error("Encre plugin SDK: EA-UI method name is required");
  const worker = workerHost();
  if (worker?.eaui?.call) {
    return (await worker.eaui.call(name, ...args)) as T;
  }
  const frame = installIframeBridge();
  if (!frame?.post) throw new Error("Encre plugin SDK: call() needs a worker or iframe bridge");
  if (!(EA_UI_IFRAME_RPC_ALLOWLIST as readonly string[]).includes(name)) {
    throw new Error(
      `Encre plugin SDK: '${name}' is not available to iframe providers (element-scoped RPCs require a worker)`,
    );
  }
  const id = ++_seq;
  return await new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      _pending.delete(id);
      reject(new Error(`Encre plugin SDK: '${name}' timed out`));
    }, IFRAME_CALL_TIMEOUT_MS) as unknown as number;
    _pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer });
    frame.post!("eaui.call", { id, name, args });
  });
}

export const actions = {
  /** Send a typed action to the owning plugin's backend handler. */
  send(kind: string, action: string, payload?: Record<string, unknown>, plugin?: string): void {
    if (!kind || !action) throw new Error("Encre plugin SDK: action kind and id are required");
    const worker = workerHost();
    if (worker?.actions?.send) {
      worker.actions.send(kind, plugin || worker.plugin || "", action, payload || {});
      return;
    }
    const frame = installIframeBridge();
    if (!frame?.post) throw new Error("Encre plugin SDK: actions.send() needs a worker or iframe bridge");
    frame.post("action", { kind, action, payload: payload || {} });
  },
};

/** Subscribe to host events (`ui_result`, `contributions_changed`, `eaui_event`). */
export function on(eventName: string, cb: (detail: unknown) => void): void {
  const worker = workerHost();
  if (worker?.on) {
    worker.on(eventName, cb);
    return;
  }
  const frame = installIframeBridge();
  if (!frame) throw new Error("Encre plugin SDK: on() needs a host bridge");
  let set = _iframeListeners.get(eventName);
  if (!set) {
    set = new Set();
    _iframeListeners.set(eventName, set);
  }
  set.add(cb);
}

function validateContribution<T>(value: PluginContribution<T>): PluginContribution<T> {
  if (!value || typeof value !== "object") throw new Error("Encre plugin SDK: contribution must be an object");
  if (!(PLUGIN_CONTRIBUTION_KINDS as readonly string[]).includes(value.kind)) {
    throw new Error(`Encre plugin SDK: unknown contribution kind '${String(value.kind)}'`);
  }
  if (!value.spec || typeof value.spec !== "object" || Array.isArray(value.spec)) {
    throw new Error("Encre plugin SDK: contribution.spec must be an object");
  }
  return value;
}

export const ui = {
  settingsSection(input: { title: string; icon?: string; rows: PluginSettingsRow[] }): PluginContribution {
    return validateContribution({ kind: "settings", spec: { page: "general", ...input } });
  },
  settingsPage(input: { pageId: string; title: string; icon?: string; rows: PluginSettingsRow[] }): PluginContribution {
    return validateContribution({
      kind: "settings",
      spec: { page: "new", page_id: input.pageId, title: input.title, icon: input.icon, rows: input.rows },
    });
  },
  contextMenu(input: { label: string; surface?: string; icon?: string; danger?: boolean; action?: string }): PluginContribution {
    return validateContribution({ kind: "context_menu", spec: { ...input } });
  },
  searchMode(input: { id: string; label: string; description?: string; icon?: string }): PluginContribution {
    return validateContribution({ kind: "search_mode", spec: { ...input } });
  },
  theme(css: string): PluginContribution {
    return validateContribution({ kind: "theme", spec: { css } });
  },
  override(input: { slot: string; template: string; priority?: number }): PluginContribution {
    if (!input.slot || !input.template) throw new Error("Encre plugin SDK: override needs slot + template");
    return validateContribution({ kind: "ui_override", spec: { ...input } });
  },
  jsModule(input: { path?: string; code?: string }): PluginContribution {
    if (!input.path && !input.code) throw new Error("Encre plugin SDK: jsModule needs path or code");
    return validateContribution({ kind: "js_module", spec: { ...input } });
  },
};

/**
 * Define a worker/iframe plugin UI.  Worker modules call this at module scope;
 * iframe providers can use the same file (render() is skipped where the host
 * owns the document).
 */
export function definePluginUi(definition: PluginUiDefinition): PluginUiDefinition {
  if (!definition || typeof definition !== "object") throw new Error("Encre plugin SDK: definition is required");
  if (definition.onAction) {
    on("ui_result", (detail) => definition.onAction?.({ kind: "ui_result", data: detail }));
    on("eaui_event", (detail) => definition.onAction?.({ kind: "eaui_event", data: detail }));
  }
  if (typeof definition.render === "function" && isWorkerRuntime()) {
    render(definition.render());
  }
  if (definition.slots) {
    for (const [slot, producer] of Object.entries(definition.slots)) {
      if (typeof producer === "function") setSlot(slot, producer());
    }
  }
  return definition;
}
