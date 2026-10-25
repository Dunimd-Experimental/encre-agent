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
 * Plugin UI bridge.
 *
 * Renders plugin-contributed frontend components (``ui_providers``) into the
 * renderer.  The backend pushes a ``plugin_ui_sync`` event on every WS
 * connection; this module keeps a registry of the providers and mounts each
 * into its ``mount_point`` container.
 *
 * Each provider is rendered inside a sandboxed ``<iframe>`` loaded from the
 * backend's ``/plugin-assets/<name>/<path>`` route, proxied through the
 * Electron main process's ``plugin-asset://`` scheme (the renderer keeps its
 * backend traffic on the encrypted WebSocket), served by the same HTTP server as the
 * admin endpoints).  For HTML entry points the document is fetched and
 * re-injected via ``srcdoc`` with a ``<base>`` tag so relative JS/CSS resolve
 * back to the plugin asset root; a lightweight ``encre`` bridge is exposed
 * inside the iframe so plugin code can talk to the renderer.
 */

import { PluginUiProvider } from "../core/types.js";
import { PLUGIN_ASSET_ORIGIN as PLUGIN_BASE } from "../ui/plugin_assets.js";
import { guardAsync } from "../core/ui_boundary.js";
import { createPluginSurface, disposePluginState, type PluginSurfaceHandle } from "../core/plugin_surface.js";
import { compilePluginHtml } from "../core/plugin_html.js";
import { setDynamicSlotOverride } from "../core/ui_slots.js";
import { sendPluginUiAction, onPluginUiResult, CONTRIBUTIONS_CHANGED } from "./plugin_contributions.js";
import { EA_UI_IFRAME_RPC_ALLOWLIST } from "../plugin_sdk/contract.js";
import { resolveEaUiCall } from "../ui/ea_ui.js";

interface MountedProvider {
  key: string;
  plugin: string;
  providerName: string;
  provider: PluginUiProvider;
  surface: PluginSurfaceHandle;
  frame: HTMLIFrameElement;
}

const _mounted = new Map<string, MountedProvider>();
const _frames = new Map<Window, MountedProvider>();

/** Handler for messages posted by plugin iframes via ``window.encre.post``. */
export type PluginMessageHandler = (
  name: string,
  type: string,
  payload: unknown,
  plugin?: string,
) => void;

const _pluginMessageHandlers = new Set<PluginMessageHandler>();

/**
 * Subscribe to messages sent from plugin iframes.  Returns an unsubscribe
 * function.  Every plugin message is also re-dispatched as a DOM
 * ``encre-plugin-message`` CustomEvent so non-TS consumers can listen.
 */
export function onPluginMessage(handler: PluginMessageHandler): () => void {
  _pluginMessageHandlers.add(handler);
  return () => {
    _pluginMessageHandlers.delete(handler);
  };
}

if (typeof window !== "undefined") {
  window.addEventListener("message", (event) => {
    const data = event.data as { source?: string; name?: string; type?: string; payload?: unknown } | null;
    if (!data || data.source !== "encre-plugin") return;
    // Never trust `data.name`: a plugin iframe could claim another plugin's
    // identity.  Resolve the sender from the frame we mounted ourselves.
    const record = event.source ? _frames.get(event.source as Window) : undefined;
    if (!record) return;
    const name = record.providerName;
    const plugin = record.plugin;
    if (!name) return;
    const type = String(data.type || "");
    const postToFrame = (message: Record<string, unknown>): void => {
      try {
        record.frame.contentWindow?.postMessage({ source: "encre-plugin-host", ...message }, "*");
      } catch {
        // Frame gone: the result has nowhere to go.
      }
    };
    if (type === "eaui.call") {
      const payload = (data.payload && typeof data.payload === "object" ? data.payload : {}) as {
        id?: unknown;
        name?: unknown;
        args?: unknown;
      };
      if (typeof payload.name !== "string" || !payload.name) return;
      if (!(EA_UI_IFRAME_RPC_ALLOWLIST as readonly string[]).includes(payload.name)) {
        postToFrame({ type: "eaui.result", id: payload.id, ok: false, error: `method '${payload.name}' is not available to iframe providers` });
        return;
      }
      // Detached root: element-scoped methods cannot reach the host DOM from
      // an iframe.  The allowlist above is already selector-free, so a
      // detached root is the safe context for the few methods that inspect it.
      const root = document.createElement("div");
      void resolveEaUiCall(payload.name, Array.isArray(payload.args) ? payload.args : [], {
        root,
        emit: (evtName, detail) => postToFrame({ type: "event", name: evtName, detail }),
      }).then(
        (value) => postToFrame({ type: "eaui.result", id: payload.id, ok: true, value }),
        (err) => postToFrame({ type: "eaui.result", id: payload.id, ok: false, error: String((err as Error)?.message || err) }),
      );
      return;
    }
    if (type === "ui.setSlot") {
      const payload = (data.payload && typeof data.payload === "object" ? data.payload : {}) as {
        slot?: unknown;
        html?: unknown;
      };
      if (typeof payload.slot !== "string" || !payload.slot || typeof payload.html !== "string" || !payload.html) return;
      setDynamicSlotOverride(payload.slot, plugin, () =>
        compilePluginHtml(payload.html as string, { stripIds: true }),
      );
      window.dispatchEvent(new CustomEvent(CONTRIBUTIONS_CHANGED));
      return;
    }
    if (type === "action") {
      const payload = (data.payload && typeof data.payload === "object" ? data.payload : {}) as {
        kind?: unknown;
        action?: unknown;
        payload?: unknown;
      };
      if (typeof payload.kind !== "string" || !payload.kind || typeof payload.action !== "string" || !payload.action) return;
      sendPluginUiAction(
        payload.kind,
        plugin,
        payload.action,
        (payload.payload && typeof payload.payload === "object" ? payload.payload : {}) as Record<string, unknown>,
      );
      return;
    }
    for (const handler of _pluginMessageHandlers) {
      try {
        handler(name, String(data.type || ""), data.payload, plugin);
      } catch {
        // A faulty handler must not break the message bus.
      }
    }
    window.dispatchEvent(new CustomEvent("encre-plugin-message", {
      detail: { ...data, name, plugin },
    }));
  });
}

function anchorFor(mountPoint: string): HTMLElement | null {
  const normalized = (mountPoint || "panel").trim();
  switch (normalized) {
    case "sidebar":
      return document.getElementById("sidebar");
    case "chat-overlay":
    case "panel":
      return document.getElementById("chat-view");
    case "settings":
      return document.getElementById("settings-view") ?? document.getElementById("settings");
    case "search":
      return document.getElementById("search-view");
    case "agents":
      return document.getElementById("agents-view");
    default:
      return document.getElementById("app");
  }
}

function frameFor(provider: PluginUiProvider): HTMLIFrameElement {
  const frame = document.createElement("iframe");
  frame.className = "encre-plugin-frame";
  frame.setAttribute("frameborder", "0");
  frame.setAttribute("scrolling", "auto");
  frame.sandbox = "allow-scripts allow-forms";
  frame.style.border = "0";
  frame.style.width = "100%";
  frame.style.height = "100%";
  frame.style.borderRadius = "8px";
  return frame;
}

/**
 * Re-inject plugin HTML through ``srcdoc`` with a ``<base>`` element so that
 * relative ``src``/``href`` inside the plugin document resolve against the
 * backend plugin-assets root, then inject the ``encre`` message bridge.
 */
async function loadHtmlIntoFrame(frame: HTMLIFrameElement, provider: PluginUiProvider): Promise<void> {
  try {
    const res = await fetch(PLUGIN_BASE + provider.assets_url);
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    let html = await res.text();
    const baseDir = provider.assets_url.replace(/[^/]*$/, "");
    const base = `<base href="${PLUGIN_BASE}${baseDir}">`;
    html = html.replace(/<head[^>]*>/i, (m: string) => `${m}${base}`);
    if (!/<head/i.test(html)) {
      html = "<head>" + base + "</head>" + html;
    }
    html = html.replace(
      "</body>",
      `<script>
        window.encre = {
          base: "${PLUGIN_BASE}",
          provider: ${JSON.stringify(provider)},
          post: (type, payload) => {
            parent.postMessage({ source: "encre-plugin", name: ${JSON.stringify(provider.name)}, type, payload }, "*");
          }
        };
      <\/script></body>`,
    );
    frame.srcdoc = html;
  } catch (err) {
    frame.srcdoc = `<!doctype html><html><head><style>body{font:12px sans-serif;color:#c33;padding:8px}</style></head><body>Plugin UI failed to load: ${String(err)}</body></html>`;
  }
}

/** Mount one provider into its mount-point container (idempotent). */
async function mount(provider: PluginUiProvider): Promise<void> {
  if (!provider || !provider.name || !provider.assets_url) {
    return;
  }
  const plugin = provider.plugin || provider.name;
  const providerName = provider.provider_id || provider.name;
  const mountPoint = provider.mount_point || "panel";
  const key = `${plugin}:${mountPoint}:${providerName}`;
  if (_mounted.has(key)) {
    return;
  }
  const anchor = anchorFor(mountPoint);
  if (!anchor) {
    return;
  }
  ensurePluginUiEventBridge();
  const surface = createPluginSurface({
    plugin,
    surface: `iframe:${mountPoint}:${providerName}`,
    kind: "iframe",
    display: "block",
    showFault: true,
  });
  const host = surface.host;
  host.classList.add("encre-plugin-container");
  host.dataset.plugin = plugin;
  host.dataset.provider = providerName;
  host.dataset.mount = mountPoint;
  host.style.display = "block";
  host.style.width = "100%";
  host.style.height = "100%";
  host.style.contain = "content";
  if (provider.title) {
    const header = document.createElement("div");
    header.className = "encre-plugin-title";
    header.style.cssText =
      "font-size:11px;color:var(--text-muted,#888);padding:2px 8px;text-transform:uppercase;letter-spacing:.05em;";
    header.textContent = provider.title;
    host.appendChild(header);
  }
  const frame = frameFor(provider);
  const body = document.createElement("div");
  body.style.cssText = "width:100%;height:calc(100% - 18px);position:relative;";
  body.appendChild(frame);
  host.appendChild(body);
  anchor.appendChild(host);
  const record: MountedProvider = { key, plugin, providerName, provider, surface, frame };
  _mounted.set(key, record);
  // Register the frame window before loading so early `encre.post` calls are
  // attributed to this provider instead of being dropped.
  const frameWindow = frame.contentWindow;
  if (frameWindow) _frames.set(frameWindow, record);
  try {
    if (/\.html$/i.test(provider.entry_point)) {
      await loadHtmlIntoFrame(frame, provider);
    } else {
      frame.src = PLUGIN_BASE + provider.assets_url;
    }
  } catch (e) {
    // Keep the host and show its local fault state: a broken plugin must be
    // visible, and its neighbours must keep working.
    const message = e instanceof Error ? e.message : String(e);
    surface.showFault(`Plugin UI failed to load: ${message || "unknown error"}`);
  }
}

function unmount(record: MountedProvider): void {
  if (record.frame.contentWindow) _frames.delete(record.frame.contentWindow);
  record.surface.dispose();
  _mounted.delete(record.key);
}

let _eventBridgeInstalled = false;
function ensurePluginUiEventBridge(): void {
  if (_eventBridgeInstalled) return;
  _eventBridgeInstalled = true;
  onPluginUiResult((result) => {
    for (const record of _mounted.values()) {
      if (record.plugin !== result.plugin) continue;
      try {
        record.frame.contentWindow?.postMessage(
          { source: "encre-plugin-host", type: "event", name: "ui_result", detail: result },
          "*",
        );
      } catch {
        // Frame gone.
      }
    }
  });
  window.addEventListener(CONTRIBUTIONS_CHANGED, () => {
    for (const record of _mounted.values()) {
      try {
        record.frame.contentWindow?.postMessage(
          { source: "encre-plugin-host", type: "event", name: "contributions_changed", detail: null },
          "*",
        );
      } catch {
        // Frame gone.
      }
    }
  });
}

/** Accept a fresh provider list from the backend; mount new providers. */
export function syncPluginUiProviders(providers: PluginUiProvider[]): void {
  const activeKeys = new Set<string>();
  for (const provider of providers || []) {
    if (!provider || !provider.name) continue;
    const plugin = provider.plugin || provider.name;
    const providerName = provider.provider_id || provider.name;
    const key = `${plugin}:${provider.mount_point || "panel"}:${providerName}`;
    activeKeys.add(key);
    // A plugin frontend that fails to mount is contained to its own container.
    void guardAsync("plugin-ui", key, () => mount(provider));
  }
  // Drop providers that are no longer advertised.
  for (const [key, record] of Array.from(_mounted)) {
    if (!activeKeys.has(key)) unmount(record);
  }
}

/**
 * Unmount every plugin UI container (hot reload).
 *
 * Called on `plugin_changed`: plugin code may have changed on disk while
 * its name stayed the same, so same-name providers cannot be diffed —
 * everything is torn down and the follow-up `plugin_ui_sync` re-mounts
 * the fresh set from the reloaded assets.
 */
export function resetPluginUi(): void {
  const plugins = new Set<string>();
  for (const record of Array.from(_mounted.values())) {
    plugins.add(record.plugin);
    unmount(record);
  }
  // A lifecycle change / hot reload replaces plugin code: carrying its old
  // state forward would let stale plugin instances observe each other.
  for (const plugin of plugins) disposePluginState(plugin);
}