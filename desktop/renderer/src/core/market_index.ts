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
 * The central-repository (ea-cwh) index plane, held for late subscribers.
 *
 * The backend sends this catalogue exactly twice at most: once when a
 * connection registers (after plugin warmup) and once more whenever the
 * daily refresh changes the index fingerprint.  A view that subscribes from
 * an event listener it only installs when it opens — the plugin market —
 * therefore misses the frame it was created for and waits forever for a
 * second one that only comes after 24h.
 *
 * `stream.ts` writes every frame it decodes in here as well as dispatching
 * the DOM event, so the payload outlives the moment it arrived: any surface
 * can read the current index the instant it opens.  Nothing is cached to
 * disk and nothing is invented — an empty `plugins` list is the honest
 * content of an index that has not published anything yet.
 */

import type { MarketCatalogEntry } from "./types.js";

/** The last index payload the backend delivered, `null` when none has. */
export interface MarketIndex {
  /** `"index"` (installed ea-cwh), `"cache"` (last known-good) or `"none"`. */
  source: string;
  indexVersion: string;
  generatedAt: string;
  plugins: MarketCatalogEntry[];
}

let _index: MarketIndex | null = null;

/** Record the frame the stream just decoded (called from `stream.ts`). */
export function setMarketIndex(payload: {
  source?: string;
  index_version?: string;
  generated_at?: string;
  plugins?: MarketCatalogEntry[];
}): void {
  _index = {
    source: payload.source || "none",
    indexVersion: payload.index_version || "",
    generatedAt: payload.generated_at || "",
    plugins: payload.plugins || [],
  };
}

/** The index as it stands now, or `null` while the first frame is outstanding. */
export function marketIndex(): MarketIndex | null {
  return _index;
}
