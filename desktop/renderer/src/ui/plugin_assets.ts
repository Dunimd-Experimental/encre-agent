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
 * The plugin asset root the renderer reads, and the one way a plugin's own
 * icon is turned into markup.
 *
 * A plugin ships its files inside its package directory and the backend
 * serves them at ``/plugin-assets/<plugin>/<relpath>`` (see
 * :meth:`encre.server.app.EncreServer._serve_plugin_asset`). The renderer
 * reaches them through the ``plugin-asset://`` scheme, handled in the main
 * process, so it never opens a plaintext HTTP channel to Python itself.  Every surface
 * that names a plugin — the market card, the fault card, a plugin-contributed
 * panel — renders its icon through :func:`pluginIconHtml` below, so one
 * declaration cannot look like three different things across the app.
 *
 * There is deliberately no default glyph.  A plugin's icon is the plugin's
 * own; when it declares none, or declares one that cannot resolve, the slot
 * stays empty.  Substituting a generic puzzle piece would be the app
 * inventing an identity for somebody else's package.
 */

/** Origin used to reach plugin-bundled files from the ``file://`` renderer.
 *  The Electron main process owns this scheme and proxies it to the loopback
 *  backend, so the renderer never opens a plaintext HTTP channel to Python —
 *  its own backend traffic stays on the encrypted WebSocket. */
export const PLUGIN_ASSET_ORIGIN = "plugin-asset://assets";

/** File extensions the backend will serve out of a plugin's ``assets/`` root. */
const IMAGE_SUFFIXES = /\.(svg|png|webp|gif|jpe?g|ico)$/i;

function esc(text: string): string {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** True when an ``icon`` declaration names a file the plugin ships itself. */
export function isPluginAssetIcon(icon: string): boolean {
  const value = (icon || "").trim();
  return value.includes("/") || IMAGE_SUFFIXES.test(value);
}

/**
 * The absolute URL of one file inside a plugin's package.
 *
 * The name is path-segment encoded (a manifest name is a plain slug, but a
 * crafted one must not be able to escape its own plugin's asset root); the
 * relative path is left intact so nested ``assets/`` subdirectories resolve.
 */
export function pluginAssetUrl(pluginName: string, relpath: string): string {
  const name = encodeURIComponent((pluginName || "").trim());
  const rel = (relpath || "").trim().replace(/^\/+/, "");
  return `${PLUGIN_ASSET_ORIGIN}/plugin-assets/${name}/${rel}`;
}

/**
 * Markup for a plugin-declared icon.
 *
 * Accepts the three forms a manifest may declare (see
 * :attr:`encre.plugins.types.PluginManifest.icon`):
 *
 *   - a path inside the package (``assets/icon.svg``) → ``<img>``;
 *   - a kebab-case lucide icon name (``message-square``) → ``<i data-lucide>``;
 *   - a single emoji → plain text, because lucide renders nothing for it.
 *
 * Anything else — blank, malformed, or an undeclared icon — renders nothing
 * at all.  See the module note on why there is no fallback glyph.
 *
 * SVG goes through ``<img>`` and never into the DOM as markup: an ``<img>``
 * loads SVG as an isolated document, so a script inside a third-party icon
 * cannot run, which inlining would allow.
 */
export function pluginIconHtml(
  icon: string | undefined,
  pluginName: string,
  opts: { emojiClass?: string; extraClass?: string } = {},
): string {
  const value = (icon || "").trim();
  if (!value) return "";
  const cls = opts.extraClass ? ` ${opts.extraClass}` : "";
  if (isPluginAssetIcon(value)) {
    const src = esc(pluginAssetUrl(pluginName, value));
    // A missing file must not leave a broken-image icon on screen either:
    // the element removes itself, and the slot stays empty like any other
    // undeclared icon.
    return `<img class="plugin-icon${cls}" src="${src}" alt="" draggable="false" `
      + `onerror="this.remove()">`;
  }
  if (/^[a-z0-9]+(-[a-z0-9]+)*$/.test(value)) {
    return `<i data-lucide="${esc(value)}" class="lucide${cls}"></i>`;
  }
  const codepoints = Array.from(value);
  const emoji = codepoints.length === 1 && (codepoints[0].codePointAt(0) || 0) > 0x2000;
  if (!emoji) return "";
  return `<span class="${opts.emojiClass || "plugin-emoji"}">${esc(value)}</span>`;
}
