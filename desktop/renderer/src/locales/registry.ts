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

import type { LocaleMessages } from "../features/i18n.js";
import { zh } from "./zh.js";
import { en } from "./en.js";

/**
 * Eagerly bundled locale tables.
 *
 * Only zh + en live in the entry chunk: zh is the default locale and en is the
 * table `t()` falls back to for a missing key, so both must be resolvable
 * synchronously.  Everything else is loaded on demand — see
 * LAZY_LOCALE_LOADERS for why.
 */
export const LOCALE_REGISTRY = {
  zh: zh,
  en: en,
} as const satisfies Record<string, LocaleMessages>;

/**
 * On-demand locale tables.
 *
 * Each locale module is a single large object literal (60–190 KB: ar/he/ja/ko
 * are the biggest).  Importing all eleven eagerly put ~1.1 MB — roughly a third
 * of the whole boot bundle — on the startup critical path, and every one of
 * them had to be parsed and evaluated before the first frame could be painted,
 * even though a session renders exactly one locale.  They are therefore split
 * into their own chunks and fetched when the locale is actually selected.
 */
export const LAZY_LOCALE_LOADERS = {
  "zh-Hant": () => import("./zh-Hant.js").then((m) => m.zhHant),
  ja: () => import("./ja.js").then((m) => m.ja),
  ko: () => import("./ko.js").then((m) => m.ko),
  de: () => import("./de.js").then((m) => m.de),
  es: () => import("./es.js").then((m) => m.es),
  pt: () => import("./pt.js").then((m) => m.pt),
  tr: () => import("./tr.js").then((m) => m.tr),
  ar: () => import("./ar.js").then((m) => m.ar),
  he: () => import("./he.js").then((m) => m.he),
} as const satisfies Record<string, () => Promise<LocaleMessages>>;

export const ALL_LOCALES: readonly string[] = ['zh', 'en', 'zh-Hant', 'ja', 'ko', 'de', 'es', 'pt', 'tr', 'ar', 'he'];
