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
 * 
 * DISCLAIMER: Users must comply with applicable AI regulations.
 * Non-compliance may result in service termination or legal liability.
 */

/**
 * Internationalization (i18n) subsystem.
 *
 * Provides the translation core for the renderer: a `t()` string getter with
 * `{placeholder}` interpolation and a two-tier locale fallback (current locale
 * then English), cached lookups, plus DOM binding helpers that refresh text,
 * placeholders and titles on any element carrying `data-i18n*` attributes.
 */

import { getState, subscribe, setSettings } from "../core/state.js";
import { LOCALE_REGISTRY, LAZY_LOCALE_LOADERS } from "../locales/registry.js";

// esbuild --define:BUILD_LOCALES=["zh","en",...] — set at build time.
// Falls back to ALL registry locales when undefined (local dev / no flag).
const BUILD_LOCALES: readonly string[] = ["zh", "en", "zh-Hant", "ja", "ko", "de", "es", "pt", "tr", "ar", "he"] as const;

/** All UI locales supported by the renderer. */
export type Locale = "zh" | "en" | "zh-Hant" | "ja" | "ko" | "de" | "es" | "pt" | "tr" | "ar" | "he";

/** Stable list of supported locales (display order in the language picker).
 *  Grouped: CJK first, then Latin-script European, then RTL. */
export const LOCALES = BUILD_LOCALES as readonly Locale[];

export interface LocaleMessages {
  [key: string]: string | LocaleMessages;
}

/** Maps a UI locale to its BCP-47 tag for Intl APIs and the `<html lang>` attribute. */
export function intlTagOf(locale: Locale): string {
  switch (locale) {
    case "zh": return "zh-CN";
    case "zh-Hant": return "zh-TW";
    case "en": return "en-US";
    default: return locale;
  }
}

/** BCP-47 tag of the currently active locale (for toLocaleString / toLocaleDateString). */
export function getIntlLocale(): string {
  return intlTagOf(getLocale());
}

// Only the eagerly bundled tables (zh + en) start out populated.  Every other
// locale is installed by ensureLocale() when it is selected — see
// LAZY_LOCALE_LOADERS for why the rest are not in the entry chunk.
const translations: Partial<Record<Locale, LocaleMessages>> = { ...LOCALE_REGISTRY };

let currentLocale: Locale = "zh";

type LocaleChangeCallback = (locale: Locale) => void;
const localeChangeCallbacks: Set<LocaleChangeCallback> = new Set();

const _localeLoads = new Map<Locale, Promise<void>>();

/**
 * Ensures the translation table for `locale` is loaded.
 *
 * zh/en are always present; every other locale is fetched from its own chunk on
 * first use.  Resolves immediately when the table is already installed, and
 * never rejects — a failed load leaves the English fallback in place so the UI
 * stays readable instead of rendering raw keys.
 */
export async function ensureLocale(locale: Locale): Promise<void> {
  if (Object.keys(translations[locale] ?? {}).length > 0) return;
  const loader = (LAZY_LOCALE_LOADERS as Record<string, (() => Promise<LocaleMessages>) | undefined>)[locale];
  if (!loader) return;
  let pending = _localeLoads.get(locale);
  if (!pending) {
    pending = loader()
      .then((messages) => {
        translations[locale] = messages;
        clearLocaleCache();
      })
      .catch(() => { /* keep the en fallback */ })
      .finally(() => { _localeLoads.delete(locale); });
    _localeLoads.set(locale, pending);
  }
  await pending;
}

/** Sets the active locale, persists it and notifies subscribers. */
export function setLocale(locale: Locale): void {
  currentLocale = locale;
  localStorage.setItem("encre-locale", locale);
  document.documentElement.setAttribute("data-locale", locale);
  document.documentElement.lang = intlTagOf(locale);
  // RTL: ar/he get a full mirrored layout. The UI now uses logical CSS
  // properties (inset-inline-*, margin-inline-*, …), so dir="rtl" mirrors
  // the shell and panels correctly while LTR locales are unaffected.
  document.documentElement.dir = locale === "ar" || locale === "he" ? "rtl" : "ltr";
  clearLocaleCache();
  localeChangeCallbacks.forEach((cb) => cb(locale));
  // Lazy tables: a locale outside zh/en resolves a moment later.  Re-run the
  // change callbacks once it lands so the UI switches to the selected language
  // instead of staying on the English fallback of that first pass.
  if (Object.keys(translations[locale] ?? {}).length === 0) {
    void ensureLocale(locale).then(() => {
      if (currentLocale !== locale) return;
      clearLocaleCache();
      localeChangeCallbacks.forEach((cb) => cb(locale));
    });
  }
}

/** Registers a callback fired whenever the locale changes; returns an unsubscribe fn. */
export function onLocaleChange(cb: LocaleChangeCallback): () => void {
  localeChangeCallbacks.add(cb);
  return () => localeChangeCallbacks.delete(cb);
}

/** Returns the currently active locale. */
export function getLocale(): Locale {
  return currentLocale;
}

/** Initializes the locale from storage/settings, falling back to browser language then `"zh"`. */
export function initLocale(): void {
  const stored = localStorage.getItem("encre-locale") as Locale | null;
  if (stored && (LOCALES as readonly string[]).includes(stored)) {
    setLocale(stored);
    try {
      setSettings({ ...getState().settings, language: stored });
    } catch {}
    return;
  }
  try {
    const st = getState();
    const settingsLang = st.settings.language as Locale | undefined;
    if (settingsLang && (LOCALES as readonly string[]).includes(settingsLang)) {
      setLocale(settingsLang);
      return;
    }
  } catch {}
  // Fall back to browser language
  setLocale(localeFromBrowser(navigator.language ?? ""));
}

/** Maps a browser language tag to the closest supported UI locale. */
export function localeFromBrowser(tag: string): Locale {
  const t = (tag || "").toLowerCase();
  if (t.startsWith("zh")) {
    return t.includes("tw") || t.includes("hk") || t.includes("mo") || t.includes("hant") ? "zh-Hant" : "zh";
  }
  if (t.startsWith("ja")) return "ja";
  if (t.startsWith("ko")) return "ko";
  if (t.startsWith("de")) return "de";
  if (t.startsWith("tr")) return "tr";
  if (t.startsWith("es")) return "es";
  if (t.startsWith("pt")) return "pt";
  if (t.startsWith("ar")) return "ar";
  if (t.startsWith("he") || t.startsWith("iw")) return "he";
  return "en";
}

type StringGetter = () => string;

const pendingSubscribers: Set<StringGetter> = new Set();
const stringCache: Map<string, string> = new Map();

/**
 * Translates a dotted key (e.g. `chat.send`) with optional `{param}` interpolation.
 *
 * @param key    - Dotted translation key.
 * @param params - Optional map of placeholder name → value.
 * @returns The translated string, or the key itself when no translation exists.
 */
export function t(key: string, params?: Record<string, string | number>): string {
  const cacheKey = params ? `${currentLocale}:${key}:${JSON.stringify(params)}` : `${currentLocale}:${key}`;
  if (stringCache.has(cacheKey)) {
    return stringCache.get(cacheKey)!;
  }

  const keys = key.split(".");
  const lookup = (locale: Locale): unknown => {
    let value: unknown = translations[locale];
    for (const k of keys) {
      if (value && typeof value === "object" && k in value) {
        value = (value as LocaleMessages)[k];
      } else {
        return undefined;
      }
    }
    return value;
  };

  let value = lookup(currentLocale);
  if (typeof value !== "string" && currentLocale !== "en") {
    value = lookup("en");
  }

  let result = typeof value === "string" ? value : key;

  if (params) {
    for (const [k, v] of Object.entries(params)) {
      result = result.replace(new RegExp(`\\{${k}\\}`, "g"), String(v));
    }
  }

  stringCache.set(cacheKey, result);
  return result;
}

/** Registers a getter invoked whenever the locale cache is cleared. */
export function subscribeLocale(fn: StringGetter): () => void {
  pendingSubscribers.add(fn);
  return () => pendingSubscribers.delete(fn);
}

/** Clears the translation cache and re-runs all pending locale subscribers. */
export function clearLocaleCache(): void {
  stringCache.clear();
  pendingSubscribers.forEach((fn) => fn());
}

/** Applies translations to all `data-i18n*` elements in the document. */
export function applyI18n(): void {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (key) {
      el.textContent = t(key);
    }
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const key = el.getAttribute("data-i18n-placeholder");
    if (key && "placeholder" in (el as HTMLInputElement)) {
      (el as HTMLInputElement).placeholder = t(key);
    }
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const key = el.getAttribute("data-i18n-title");
    if (key) {
      el.setAttribute("data-tooltip", t(key));
    }
  });
}

/** Replaces the translation table for a single locale and clears the cache. */
export function loadTranslations(locale: Locale, messages: LocaleMessages): void {
  translations[locale] = messages;
  clearLocaleCache();
}

/** Installs every provided locale table at once and clears the cache. */
export function loadAllTranslations(messages: Partial<Record<Locale, LocaleMessages>>): void {
  for (const loc of LOCALES) {
    const table = messages[loc];
    // Skip empty/absent tables so they cannot masquerade as "loaded" and stop
    // ensureLocale() from fetching the real chunk later.
    if (table && Object.keys(table).length > 0) {
      translations[loc] = table;
    }
  }
  clearLocaleCache();
}

/** Creates a cached string getter bound to a fixed translation key. */
export function createLocaleGetter(key: string): StringGetter {
  return () => t(key);
}
