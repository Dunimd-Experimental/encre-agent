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
 */

/**
 * Plugin JS runtime.
 *
 * Plugins may ship real JavaScript (contribution kind `js_module`).  The
 * code runs inside a dedicated Web Worker — it has NO DOM, NO network and
 * NO access to app state; it can only call the capability API injected as
 * the global `Encre`:
 *
 *   Encre.ui.render(html)                  -> own root container (sanitized)
 *   Encre.ui.setSlot(slot, html)           -> take over a UI slot dynamically
 *   Encre.actions.send(kind, plugin, act, payload) -> unified plugin channel
 *   Encre.on("ui_result" | "contributions_changed" | "eaui_event", cb)
 *   Encre.eaui.call(name, ...args)         -> EA-UI registry RPC (Promise)
 *   Encre.console.log/warn/error
 *
 * Isolation guarantees:
 *   - A worker crash, infinite loop or throwing module can never take down
 *     the renderer: the main thread only reacts to messages, every handler
 *     is guarded, and workers are rebuilt from scratch on resync.
 *   - `Encre.eaui.call` forwards JSON-only args to the EA-UI registry; element
 *     references are selectors resolved strictly inside the plugin's own root
 *     container, and callbacks arrive back as `eaui_event` messages.
 *   - HTML sent by a worker is sanitized before insertion (scripts, inline
 *     event handlers and javascript: URLs are stripped) and can only land
 *     in the plugin's own root container or a registered slot.  The stable
 *     `ea-ui-*` class aliases (see ui/ea_ui.ts) are resolved to first-party
 *     classes after sanitization, so worker markup inherits the app look.
 *   - The backend->frontend transport stays the unified encrypted WS; the
 *     worker reaches it only through `Encre.actions.send`.
 */

import { sendPluginUiAction, onPluginUiResult, CONTRIBUTIONS_CHANGED } from "../features/plugin_contributions.js";
import { setDynamicSlotOverride, clearDynamicSlotOverrides } from "./ui_slots.js";
import { EA_UI_RPC, resolveEaUiCall, type EaUiCallContext } from "../ui/ea_ui.js";
import { attachInputExpand } from "../ui/input-expand.js";
import { reportUiFault } from "./ui_boundary.js";
import { compilePluginHtml, MAX_PLUGIN_HTML } from "./plugin_html.js";
import { createPluginSurface, disposePluginState, type PluginSurfaceHandle } from "./plugin_surface.js";

/** One `js_module` contribution as pushed inside `plugin_ui_sync`. */
export interface PluginJsModule {
  plugin: string;
  code: string;
}

const MAX_CODE_LEN = 512 * 1024;
const MAX_HTML_LEN = MAX_PLUGIN_HTML;

/** Strip anything that could execute code or navigate the host app. */

/**
 * Wire first-party widget behaviour into freshly injected plugin HTML:
 * lucide icons and the rich-textarea expand/collapse toggle (right-top
 * corner, hover reveal).  Interactive widgets (dropdown, color picker) are
 * deliberately NOT auto-bound here: the worker opts in per widget through
 * the EA-UI RPC (`select.bind` / `color-picker.bind`), which forwards every
 * pick back as an `eaui_event`.  An automatic bind would stack a second
 * listener on top of the worker's own and double-fire every pick.
 */
function activatePluginWidgets(root: ParentNode): void {
  const lucide = (window as any).lucide;
  if (lucide && typeof lucide.createIcons === "function") {
    try {
      lucide.createIcons({ root });
    } catch {
      // Icons are cosmetic; a bad glyph must not break the plugin surface.
    }
  }
  attachInputExpand(root);
}

/** Remembered draggable card positions, keyed by plugin; re-applied after
 *  every worker re-render so a moved panel survives DOM rebuilds. */
const _dragPositions = new Map<string, { x: number; y: number }>();

/**
 * Make a worker-rendered card draggable.  The card opts in declaratively:
 * its root element carries `data-eaui-draggable` and (recommended) contains
 * a `data-eaui-drag-handle` child acting as the grip.  Dragging applies the
 * position straight to the live DOM — it never round-trips through the
 * worker — and the last position is remembered so re-renders restore it.
 */
function attachPluginDrag(plugin: string, root: ParentNode): void {
  const card = root.firstElementChild as HTMLElement | null;
  if (!card || !card.hasAttribute("data-eaui-draggable")) return;
  const handle = card.querySelector<HTMLElement>("[data-eaui-drag-handle]") ?? card;
  const saved = _dragPositions.get(plugin);
  if (saved) {
    card.style.right = "auto";
    card.style.bottom = "auto";
    card.style.left = `${saved.x}px`;
    card.style.top = `${saved.y}px`;
  }
  handle.addEventListener("pointerdown", (down) => {
    if (down.button !== 0) return;
    const target = down.target as HTMLElement | null;
    if (target?.closest?.("input, textarea, select, button, label")) return;
    down.preventDefault();
    const rect = card.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    card.style.right = "auto";
    card.style.bottom = "auto";
    card.style.left = `${rect.left}px`;
    card.style.top = `${rect.top}px`;
    const dx = down.clientX - rect.left;
    const dy = down.clientY - rect.top;
    const move = (ev: PointerEvent): void => {
      const maxX = Math.max(8, window.innerWidth - rect.width - 8);
      const maxY = Math.max(8, window.innerHeight - rect.height - 8);
      card.style.left = `${Math.min(Math.max(ev.clientX - dx, 8), maxX)}px`;
      card.style.top = `${Math.min(Math.max(ev.clientY - dy, 8), maxY)}px`;
    };
    const up = (): void => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      const r = card.getBoundingClientRect();
      _dragPositions.set(plugin, { x: r.left, y: r.top });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  });
}

/** The bootstrap executed inside every plugin worker. The `__EAUI_METHODS__`
 *  token is replaced with the EA-UI RPC method list at spawn time. */
const WORKER_PREAMBLE = `
"use strict";
const handlers = { ui_result: new Set(), contributions_changed: new Set() };
let __eauiSeq = 0;
const __eauiPending = new Map();
const Encre = {
  plugin: __PLUGIN__,
  ui: {
    render: (html) => self.postMessage({ type: "call", fn: "ui.render", args: [String(html ?? "")] }),
    setSlot: (slot, html) => self.postMessage({ type: "call", fn: "ui.setSlot", args: [String(slot ?? ""), String(html ?? "")] }),
  },
  actions: {
    send: (kind, plugin, action, payload) =>
      self.postMessage({ type: "call", fn: "actions.send", args: [kind, plugin, action, payload] }),
  },
  on: (name, cb) => {
    let set = handlers[name];
    if (!set) { set = new Set(); handlers[name] = set; }
    if (typeof cb === "function") set.add(cb);
  },
  console,
};
// EA-UI registry bridge: JSON-only args; element params are selector strings
// resolved inside this plugin's own root; callbacks arrive as "eaui_event".
Encre.eaui = {
  call: (name, ...args) => new Promise((resolve, reject) => {
    const id = ++__eauiSeq;
    __eauiPending.set(id, { resolve, reject });
    self.postMessage({ type: "call", fn: "eaui.call", args: [String(name), args, id] });
  }),
  methods: __EAUI_METHODS__,
};
self.Encre = Encre;
self.onmessage = (ev) => {
  const msg = ev.data || {};
  if (msg.type === "eaui_result") {
    const p = __eauiPending.get(msg.id);
    if (p) {
      __eauiPending.delete(msg.id);
      if (msg.ok) p.resolve(msg.value);
      else p.reject(new Error(msg.error || "eaui call failed"));
    }
    return;
  }
  if (msg.type === "event") {
    const set = handlers[msg.name];
    if (set) for (const cb of Array.from(set)) {
      try { cb(msg.detail); } catch (e) { reportUiFault("plugin-event", "handler", e); }
    }
  } else if (msg.type === "run") {
    try {
      const run = new Function("Encre", msg.code);
      run(Encre);
    } catch (e) {
      console.error("[plugin] module failed:", e);
      self.postMessage({ type: "module_error", message: String((e && e.stack) || e) });
    }
  }
};
`;

interface RuntimeWorker {
  plugin: string;
  worker: Worker;
  url: string;
}

const _workers = new Map<string, RuntimeWorker>();
let _uiResultUnsub: (() => void) | null = null;
let _contribListener: (() => void) | null = null;

const _roots = new Map<string, PluginSurfaceHandle>();

function ensureRootSurface(plugin: string): PluginSurfaceHandle {
  let handle = _roots.get(plugin);
  if (!handle || handle.disposed) {
    handle = createPluginSurface({
      plugin,
      surface: "runtime",
      kind: "runtime",
      // Shadow root: plugin `<style>` stays inside, while the app's own
      // stylesheets are adopted so `ea-ui-*` classes keep their look.
      shadow: true,
      showFault: true,
    });
    document.body.appendChild(handle.host);
    _roots.set(plugin, handle);
  }
  return handle;
}

/** Handles one guarded call coming from a plugin worker. */
function handleWorkerCall(plugin: string, worker: Worker, fn: string, args: unknown[]): void {
  switch (fn) {
    case "ui.render": {
      const html = typeof args[0] === "string" ? args[0] : "";
      if (!html || html.length > MAX_HTML_LEN) return;
      const surface = ensureRootSurface(plugin);
      if (!surface.update(html, { allowStyle: true })) return;
      activatePluginWidgets(surface.root);
      attachPluginDrag(plugin, surface.root);
      break;
    }
    case "ui.setSlot": {
      const slot = typeof args[0] === "string" ? args[0] : "";
      const html = typeof args[1] === "string" ? args[1] : "";
      if (!slot || !html || html.length > MAX_HTML_LEN) return;
      // Sanitize once before storing: the slot renderer sanitizes again, but
      // no unsanitized plugin string should be retained in the registry.
      const safe = compilePluginHtml(html, { stripIds: true });
      setDynamicSlotOverride(slot, plugin, () => safe);
      window.dispatchEvent(new CustomEvent(CONTRIBUTIONS_CHANGED));
      break;
    }
    case "actions.send": {
      const [kind, pluginId, action, payload] = args;
      if (typeof kind !== "string" || typeof pluginId !== "string" || typeof action !== "string") return;
      if (pluginId !== plugin) return; // a module may only act as its own plugin
      sendPluginUiAction(kind, plugin, action, (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>);
      break;
    }
    case "eaui.call": {
      const name = typeof args[0] === "string" ? args[0] : "";
      const callArgs = Array.isArray(args[1]) ? args[1] : [];
      const callId = typeof args[2] === "number" ? args[2] : 0;
      const ctx: EaUiCallContext = {
        root: ensureRootSurface(plugin).root,
        emit: (evtName, detail) => {
          try {
            worker.postMessage({ type: "event", name: evtName, detail });
          } catch {
            // Worker gone: nothing to deliver the event to.
          }
        },
      };
      resolveEaUiCall(name, callArgs, ctx).then(
        (value) => {
          try {
            worker.postMessage({ type: "eaui_result", id: callId, ok: true, value });
          } catch {
            // Result not cloneable: drop it, the worker's promise stays pending.
          }
        },
        (e) => {
          try {
            worker.postMessage({ type: "eaui_result", id: callId, ok: false, error: String((e as Error)?.message || e) });
          } catch {
            // Worker gone: nothing to deliver the error to.
          }
        },
      );
      break;
    }
    default:
      break;
  }
}

function spawnWorker(mod: PluginJsModule): void {
  if (_workers.has(mod.plugin)) return;
  if (typeof mod.code !== "string" || !mod.code || mod.code.length > MAX_CODE_LEN) return;
  // Inject the EA-UI RPC method list so the worker can validate names offline.
  const preamble = WORKER_PREAMBLE
    .replace("__PLUGIN__", JSON.stringify(mod.plugin))
    .replace("__EAUI_METHODS__", JSON.stringify(Object.keys(EA_UI_RPC)));
  const blob = new Blob([preamble], { type: "text/javascript" });
  const url = URL.createObjectURL(blob);
  const worker = new Worker(url);
  const entry: RuntimeWorker = { plugin: mod.plugin, worker, url };
  _workers.set(mod.plugin, entry);
  worker.onmessage = (ev: MessageEvent) => {
    const msg = ev.data || {};
    try {
      if (msg.type === "call" && Array.isArray(msg.args)) {
        handleWorkerCall(mod.plugin, worker, String(msg.fn || ""), msg.args);
      } else if (msg.type === "module_error") {
        reportUiFault("plugin-worker", mod.plugin, new Error(String(msg.message || "module error")));
      }
    } catch (e) {
      // A malformed worker message must never break the renderer.
      reportUiFault("plugin-worker", mod.plugin, e);
    }
  };
  worker.onerror = (event) => {
    // Worker-level failures (syntax errors at import, runtime panics) are
    // contained in the worker; record them in the shared fault registry.
    reportUiFault("plugin-worker", mod.plugin, new Error(event.message || "worker error"));
  };
  worker.postMessage({ type: "run", code: mod.code });
}

function terminateWorker(plugin: string): void {
  const entry = _workers.get(plugin);
  if (!entry) return;
  _workers.delete(plugin);
  try {
    entry.worker.terminate();
  } catch {
    // Already gone.
  }
  URL.revokeObjectURL(entry.url);
  _dragPositions.delete(plugin);
  clearDynamicSlotOverrides(plugin);
  _roots.get(plugin)?.dispose();
  _roots.delete(plugin);
  disposePluginState(plugin);
}

/** Signature of the current module set; rebuilds workers only on change. */
let _lastSig = "";

/**
 * Syncs the running worker set with the `js_module` contributions from the
 * latest `plugin_ui_sync`.  Workers whose module list changed are rebuilt;
 * the bridge listeners are installed lazily on first call.
 */
export function syncPluginRuntime(modules: PluginJsModule[]): void {
  const list = (modules || []).filter(
    (m) => m && typeof m.plugin === "string" && m.plugin && typeof m.code === "string" && m.code,
  );
  const sig = list.map((m) => `${m.plugin}:${m.code.length}`).join("|");
  if (sig === _lastSig) return;
  _lastSig = sig;

  const wanted = new Set(list.map((m) => m.plugin));
  for (const plugin of Array.from(_workers.keys())) {
    if (!wanted.has(plugin)) terminateWorker(plugin);
  }
  for (const mod of list) {
    if (!_workers.has(mod.plugin)) spawnWorker(mod);
  }

  if (!_uiResultUnsub) {
    _uiResultUnsub = onPluginUiResult((result) => {
      for (const { worker } of _workers.values()) {
        worker.postMessage({ type: "event", name: "ui_result", detail: result });
      }
    });
  }
  if (!_contribListener) {
    _contribListener = () => {
      for (const { worker } of _workers.values()) {
        worker.postMessage({ type: "event", name: "contributions_changed", detail: null });
      }
    };
    window.addEventListener(CONTRIBUTIONS_CHANGED, _contribListener);
  }
}

/** Stops the whole runtime (plugin system reload / teardown). */
export function stopPluginRuntime(): void {
  _lastSig = "";
  for (const plugin of Array.from(_workers.keys())) terminateWorker(plugin);
  _uiResultUnsub?.();
  _uiResultUnsub = null;
  if (_contribListener) {
    window.removeEventListener(CONTRIBUTIONS_CHANGED, _contribListener);
    _contribListener = null;
  }
}

/** Number of live plugin workers (diagnostics). */
export function pluginRuntimeCount(): number {
  return _workers.size;
}
