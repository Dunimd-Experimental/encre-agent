/**
 * Plugin surface host — the one container every plugin-authored UI lives in.
 *
 * Responsibilities:
 *   - one DOM host per (plugin, surface, instance), never a bare fragment in
 *     first-party markup;
 *   - one persistent state scope per (plugin, surface), inaccessible to other
 *     plugins and disposed only with that plugin;
 *   - a hard render boundary: bad markup, a throwing renderer or a disposed
 *     host degrades to a local fallback, never to a blank/broken host page;
 *   - automatic teardown by plugin (disable, uninstall, hot reload).
 *
 * Shadow surfaces (`shadow: true`) additionally isolate plugin `<style>` and
 * share the app's stylesheets through `adoptedStyleSheets`, so first-party
 * `ea-ui-*` classes keep working while plugin CSS cannot leak into the app.
 */

import { compilePluginHtml, escapePluginAttr, pluginDomId } from "./plugin_html.js";
import { reportUiFault } from "./ui_boundary.js";

export interface PluginSurfaceOptions {
  plugin: string;
  surface: string;
  /** Optional explicit instance key; auto-generated when omitted. */
  instance?: string;
  kind?: "runtime" | "iframe" | "settings" | "slot";
  /** Host display value.  Shadow hosts default to `contents`. */
  display?: string;
  /** Render into a shadow root (isolates plugin CSS). */
  shadow?: boolean;
  /** First-party markup used when the plugin render fails. */
  fallbackHtml?: string;
  /** Show a compact fault card when there is no fallback (default true). */
  showFault?: boolean;
}

export interface PluginStateScope {
  readonly plugin: string;
  readonly surface: string;
  readonly disposed: boolean;
  get<T = unknown>(key: string): T | undefined;
  set<T = unknown>(key: string, value: T): void;
  delete(key: string): void;
  clear(): void;
  size(): number;
  subscribe(listener: (key: string, value: unknown) => void): () => void;
  dispose(): void;
}

export interface PluginSurfaceHandle {
  readonly host: HTMLElement;
  readonly root: ParentNode;
  readonly state: PluginStateScope;
  readonly disposed: boolean;
  update(html: string, options?: { allowStyle?: boolean }): boolean;
  showFault(message: string): void;
  dispose(): void;
}

const _scopes = new Map<string, PluginStateScope>();
const _surfaces = new Map<string, Set<PluginSurfaceHandleImpl>>();
let _instanceSeq = 0;

function scopeKey(plugin: string, surface: string): string {
  return `${plugin}\u0000${surface}`;
}

function createStateScope(plugin: string, surface: string): PluginStateScope {
  const data = new Map<string, unknown>();
  const listeners = new Set<(key: string, value: unknown) => void>();
  let disposed = false;
  return {
    plugin,
    surface,
    get disposed() {
      return disposed;
    },
    get<T = unknown>(key: string): T | undefined {
      return data.get(key) as T | undefined;
    },
    set<T = unknown>(key: string, value: T): void {
      if (disposed) return;
      data.set(key, value);
      for (const fn of Array.from(listeners)) {
        try {
          fn(key, value);
        } catch (e) {
          reportUiFault("plugin-state", `${plugin}:${surface}`, e);
        }
      }
    },
    delete(key: string): void {
      if (!disposed) data.delete(key);
    },
    clear(): void {
      if (!disposed) data.clear();
    },
    size(): number {
      return data.size;
    },
    subscribe(listener: (key: string, value: unknown) => void): () => void {
      if (disposed) return () => {};
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispose(): void {
      if (disposed) return;
      disposed = true;
      data.clear();
      listeners.clear();
      _scopes.delete(scopeKey(plugin, surface));
    },
  };
}

/** Persistent, plugin-private state scope for one surface. */
export function pluginStateScope(plugin: string, surface = "default"): PluginStateScope {
  const key = scopeKey(plugin, surface);
  let scope = _scopes.get(key);
  if (!scope) {
    scope = createStateScope(plugin, surface);
    _scopes.set(key, scope);
  }
  return scope;
}

/** Drop every state scope owned by `plugin` (disable / uninstall). */
export function disposePluginState(plugin: string): void {
  for (const [key, scope] of Array.from(_scopes)) {
    if (scope.plugin === plugin) {
      scope.dispose();
      _scopes.delete(key);
    }
  }
}

function appStyleSheets(): CSSStyleSheet[] {
  const sheets: CSSStyleSheet[] = [];
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      // Cross-origin sheets throw on cssRules; keep only readable ones.
      void sheet.cssRules;
      sheets.push(sheet);
    } catch {
      // Ignore: an unreadable sheet cannot be adopted.
    }
  }
  return sheets;
}

function adoptAppStyles(shadow: ShadowRoot): void {
  try {
    const sheets = appStyleSheets();
    if (sheets.length) shadow.adoptedStyleSheets = sheets;
  } catch {
    // Older engines / edge cases: fall back to no adopted sheets.  The host
    // still inherits custom properties and the plugin's own styles remain
    // scoped to the shadow root.
  }
}

function faultMarkup(plugin: string, message: string): string {
  return `<div class="ea-plugin-fault" role="status" data-ea-plugin="${escapePluginAttr(plugin)}" style="display:flex;align-items:center;gap:6px;padding:6px 8px;font:11px/1.4 inherit;color:var(--text-muted,#888);border:1px solid var(--border-light,#333);border-radius:6px;background:var(--bg-secondary,#18181b);max-width:100%;overflow:hidden">` +
    `<span aria-hidden="true">⚠</span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapePluginAttr(message)}</span></div>`;
}

class PluginSurfaceHandleImpl implements PluginSurfaceHandle {
  readonly host: HTMLElement;
  readonly root: ParentNode;
  readonly state: PluginStateScope;
  private readonly plugin: string;
  private readonly surface: string;
  private readonly options: PluginSurfaceOptions;
  private readonly shadow: ShadowRoot | null;
  private readonly container: HTMLElement;
  private _disposed = false;
  private _lastHtml = "";

  constructor(options: PluginSurfaceOptions) {
    this.options = options;
    this.plugin = options.plugin;
    this.surface = options.surface;
    this.state = pluginStateScope(this.plugin, this.surface);
    const instance = options.instance || `p${++_instanceSeq}`;

    const host = document.createElement("div");
    host.className = `ea-plugin-surface ea-plugin-surface--${options.kind || "slot"}`;
    host.dataset.eaPlugin = this.plugin;
    host.dataset.eaSurface = this.surface;
    host.dataset.eaInstance = instance;
    host.style.display = options.display || (options.shadow ? "contents" : "block");
    if (!options.shadow) {
      host.style.isolation = "isolate";
      host.style.contain = "layout style paint";
      host.style.minWidth = "0";
    }
    this.host = host;

    if (options.shadow) {
      this.shadow = host.attachShadow({ mode: "open" });
      adoptAppStyles(this.shadow);
      const container = document.createElement("div");
      container.className = "ea-plugin-surface-root";
      container.style.display = "contents";
      this.shadow.appendChild(container);
      this.container = container;
      this.root = container;
    } else {
      this.shadow = null;
      this.container = host;
      this.root = host;
    }

    const set = _surfaces.get(this.plugin) ?? new Set();
    set.add(this);
    _surfaces.set(this.plugin, set);
  }

  get disposed(): boolean {
    return this._disposed;
  }

  update(html: string, options?: { allowStyle?: boolean }): boolean {
    if (this._disposed) return false;
    if (typeof html !== "string") {
      this.fail(new Error("plugin surface received non-string HTML"));
      return false;
    }
    try {
      const safe = compilePluginHtml(html, {
        allowStyle: options?.allowStyle ?? !!this.shadow,
        stripIds: !this.shadow,
      });
      // Re-adopt after a theme/settings change may have appended stylesheets.
      if (this.shadow) adoptAppStyles(this.shadow);
      this.container.innerHTML = safe;
      this._lastHtml = safe;
      return true;
    } catch (e) {
      this.fail(e);
      return false;
    }
  }

  showFault(message: string): void {
    if (this._disposed) return;
    this.container.innerHTML = faultMarkup(this.plugin, message);
  }

  private fail(error: unknown): void {
    reportUiFault("plugin-surface", `${this.plugin}:${this.surface}`, error);
    if (this.options.fallbackHtml) {
      try {
        this.container.innerHTML = compilePluginHtml(this.options.fallbackHtml, {
          allowStyle: !!this.shadow,
          stripIds: !this.shadow,
        });
        return;
      } catch {
        // Fall through to the compact fault card.
      }
    }
    if (this.options.showFault !== false) {
      const message = error instanceof Error ? error.message : String(error);
      this.showFault(`Plugin UI failed: ${message || "unknown error"}`);
    } else {
      this.container.textContent = "";
    }
  }

  dispose(): void {
    if (this._disposed) return;
    this._disposed = true;
    this._lastHtml = "";
    const set = _surfaces.get(this.plugin);
    if (set) {
      set.delete(this);
      if (set.size === 0) _surfaces.delete(this.plugin);
    }
    this.host.remove();
  }

  /** Diagnostics only. */
  get lastHtml(): string {
    return this._lastHtml;
  }
}

/** Create an isolated host for one plugin surface. */
export function createPluginSurface(options: PluginSurfaceOptions): PluginSurfaceHandle {
  if (!options.plugin) throw new Error("createPluginSurface: plugin is required");
  if (!options.surface) throw new Error("createPluginSurface: surface is required");
  return new PluginSurfaceHandleImpl(options);
}

/** Drop every host owned by `plugin` (disable / uninstall / hot reload). */
export function disposePluginSurfaces(plugin: string): void {
  const set = _surfaces.get(plugin);
  if (!set) return;
  for (const handle of Array.from(set)) handle.dispose();
  _surfaces.delete(plugin);
}

/** Number of live hosts for diagnostics. */
export function pluginSurfaceCount(): number {
  let n = 0;
  for (const set of _surfaces.values()) n += set.size;
  return n;
}
