/**
 * Shared plugin HTML pipeline.
 *
 * Every plugin-authored fragment — declarative slot template, worker-rendered
 * overlay, iframe srcdoc — passes through this module before touching the
 * first-party DOM.  Keeping it in one place means the sanitizer cannot drift
 * between the three entry points, and the EA-UI alias translation always
 * happens after sanitization (translation only rewrites class attributes, so
 * it cannot reintroduce stripped content).
 */

import { translateEaUiClasses } from "../ui/ea_ui.js";

export const MAX_PLUGIN_HTML = 256 * 1024;
export const MAX_PLUGIN_CSS = 64 * 1024;

export interface SanitizeOptions {
  /** Keep `<style>` elements.  Only safe inside a shadow root; light-DOM
   *  plugin fragments must not be able to restyle the host app. */
  allowStyle?: boolean;
  /** Strip `id` attributes to prevent collisions with first-party ids. */
  stripIds?: boolean;
}

const DROP_TAGS = "script,iframe,frame,object,embed,link,meta,base,form,noscript";
const URL_ATTRS = new Set(["href", "src", "xlink:href", "action", "formaction", "poster"]);
const DANGEROUS_URL = /^\s*(?:javascript|vbscript|data:text\/html)/i;

/** Remove anything that can execute code, navigate the host or replace the
 *  document.  Attribute handlers and dangerous URL schemes are stripped. */
export function sanitizePluginHtml(html: string, options: SanitizeOptions = {}): string {
  if (typeof html !== "string" || !html) return "";
  const clipped = html.length > MAX_PLUGIN_HTML ? html.slice(0, MAX_PLUGIN_HTML) : html;
  const tpl = document.createElement("template");
  tpl.innerHTML = clipped;
  tpl.content.querySelectorAll(DROP_TAGS).forEach((el) => el.remove());
  if (!options.allowStyle) {
    tpl.content.querySelectorAll("style").forEach((el) => el.remove());
  }
  tpl.content.querySelectorAll("*").forEach((el) => {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on")) {
        el.removeAttribute(attr.name);
      } else if (URL_ATTRS.has(name) && DANGEROUS_URL.test(attr.value)) {
        el.removeAttribute(attr.name);
      } else if (options.stripIds && name === "id") {
        el.removeAttribute(attr.name);
      }
    }
  });
  return tpl.innerHTML;
}

/** Sanitize, then resolve the plugin-facing `ea-ui-*` class aliases. */
export function compilePluginHtml(html: string, options: SanitizeOptions = {}): string {
  return translateEaUiClasses(sanitizePluginHtml(html, options));
}

/** Stable DOM-safe token for a plugin id (used in data attributes / ids). */
export function pluginDomId(plugin: string): string {
  const safe = String(plugin || "plugin").replace(/[^a-z0-9_-]/gi, "-");
  return safe || "plugin";
}

/** Escape one untrusted value for an HTML attribute. */
export function escapePluginAttr(text: string): string {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Keep theme contributions to their documented contract: custom-property
 * overrides on `:root` / `html` / `body`.  A theme is data, not a stylesheet
 * that may target arbitrary app selectors.
 */
export function sanitizeThemeCss(css: string): string {
  if (typeof css !== "string" || !css || css.length > MAX_PLUGIN_CSS) return "";
  if (/<\s*\/?\s*(script|style)/i.test(css)) return "";
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    const blocks: string[] = [];
    for (const rule of Array.from(sheet.cssRules)) {
      if (!(rule instanceof CSSStyleRule)) continue;
      const selector = rule.selectorText.trim().toLowerCase();
      if (![":root", "html", "body", "html[data-theme]", "html[data-theme='dark']", "html[data-theme='light']"].includes(selector)) {
        continue;
      }
      const decls: string[] = [];
      for (const prop of Array.from(rule.style)) {
        if (prop.startsWith("--")) {
          const value = rule.style.getPropertyValue(prop).trim();
          if (value && !/[;{}]/.test(value)) decls.push(`${prop}:${value}`);
        }
      }
      if (decls.length) blocks.push(`${rule.selectorText}{${decls.join(";")}}`);
    }
    return blocks.join("\n");
  } catch {
    return "";
  }
}
