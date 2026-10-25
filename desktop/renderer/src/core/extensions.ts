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
 * Unified UI extension registry.
 *
 * Every hardcoded UI fragment in the renderer is expressed as a *slot*: a
 * namespaced string (`chat.tool.meta`, `settings.panel`, `i18n.bundle`, …).
 * Built-in entries are registered with `owner: "builtin"` and the same shape
 * as plugin entries, so a plugin can add, replace or remove any built-in piece
 * of the UI through one API instead of patching a file.
 *
 * Semantics:
 *   - `register(slot, entry)` returns an unregister function.
 *   - Registering an entry with an already-used `id` replaces it (last writer
 *     wins), which is what lets a plugin override a built-in definition.
 *   - `entries(slot)` returns entries sorted by `priority` descending, with
 *     insertion order as the stable tiebreak.
 *   - `resolve(slot, ctx)` returns the first entry whose `visible(ctx)` is not
 *     `false`, so slots can express mutually-exclusive overrides.
 *   - Removal is fully live: any subscriber fires after every mutation, and
 *     consumers that cached their own list re-read it on the next render.
 */

export const BUILTIN_OWNER = "builtin";

export interface SlotEntry {
  /** Unique key within its slot. Plugins reuse a built-in id to override it. */
  id: string;
  /** `"builtin"` or a plugin name. Defaults to `"plugin"`. */
  owner?: string;
  /** Higher sorts first. Defaults to 0. */
  priority?: number;
  /** Optional human-readable title (lists, menus, inspectors). */
  title?: string;
  /** Optional description. */
  description?: string;
  /** When it returns false the entry is skipped by `resolve()`. */
  visible?: (ctx?: unknown) => boolean | undefined;
  [key: string]: unknown;
}

export type SlotChangeCallback = (slot: string) => void;

const store = new Map<string, Map<string, SlotEntry>>();
const changeListeners = new Set<SlotChangeCallback>();

function slotBucket(slot: string): Map<string, SlotEntry> {
  let bucket = store.get(slot);
  if (!bucket) {
    bucket = new Map();
    store.set(slot, bucket);
  }
  return bucket;
}

function notify(slot: string): void {
  for (const fn of changeListeners) {
    try {
      fn(slot);
    } catch {
      // A broken subscriber must never break the registry.
    }
  }
}

export const ext = {
  /** Registers an entry and returns a function that removes it. */
  register<T extends SlotEntry = SlotEntry>(slot: string, entry: T): () => void {
    const bucket = slotBucket(slot);
    const normalized: SlotEntry = {
      owner: BUILTIN_OWNER,
      priority: 0,
      ...entry,
    };
    bucket.set(entry.id, normalized as SlotEntry);
    notify(slot);
    return () => ext.unregister(slot, entry.id);
  },

  /** Removes a single entry by slot + id. Returns true when something was removed. */
  unregister(slot: string, id: string): boolean {
    const bucket = store.get(slot);
    if (!bucket || !bucket.delete(id)) return false;
    notify(slot);
    return true;
  },

  /** Removes every entry owned by the given plugin name (plugin shutdown). */
  clearOwner(owner: string): number {
    let removed = 0;
    for (const [slot, bucket] of store) {
      for (const [id, entry] of bucket) {
        if ((entry.owner ?? "plugin") === owner) {
          bucket.delete(id);
          removed++;
        }
      }
      if (removed > 0) notify(slot);
    }
    return removed;
  },

  /** Removes a whole slot (all entries). */
  clearSlot(slot: string): void {
    if (store.delete(slot)) notify(slot);
  },

  /** Removes everything. Test/introspection use only. */
  clearAll(): void {
    store.clear();
    for (const fn of changeListeners) {
      try {
        fn("*");
      } catch {
        // ignore
      }
    }
  },

  /** All entries of a slot, priority-descending, insertion-ordered ties. */
  entries<T extends SlotEntry = SlotEntry>(slot: string): T[] {
    const bucket = store.get(slot);
    if (!bucket) return [];
    const out = Array.from(bucket.values()) as unknown as T[];
    return out.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
  },

  /** First visible entry, highest priority wins. Null when nothing matches. */
  resolve<T extends SlotEntry = SlotEntry>(slot: string, ctx?: unknown): T | null {
    for (const entry of ext.entries<T>(slot)) {
      if (entry.visible && entry.visible(ctx) === false) continue;
      return entry;
    }
    return null;
  },

  /** Entry by exact id, regardless of priority or visibility. */
  find<T extends SlotEntry = SlotEntry>(slot: string, id: string): T | null {
    const bucket = store.get(slot);
    const hit = bucket?.get(id);
    return hit ? (hit as unknown as T) : null;
  },

  /** True when the slot has at least one entry. */
  has(slot: string): boolean {
    const bucket = store.get(slot);
    return !!bucket && bucket.size > 0;
  },

  /** Register a batch, returning one combined unregister. */
  registerMany<T extends SlotEntry = SlotEntry>(
    slot: string,
    entries: T[],
  ): () => void {
    const offs = entries.map((entry) => ext.register(slot, entry));
    return () => {
      for (const off of offs) off();
    };
  },

  /** Subscribes to slot mutations. Pass a slot id or `"*"` for all. */
  on(cb: SlotChangeCallback): () => void {
    changeListeners.add(cb);
    return () => changeListeners.delete(cb);
  },
};

/** Convenience: an entry factory that stamps the owning plugin on every entry. */
export function pluginScope(owner: string) {
  return <T extends SlotEntry = SlotEntry>(slot: string, entry: T): () => void =>
    ext.register(slot, { owner, ...entry });
}

/**
 * Matches a candidate string against a matcher spec. Used by the tool,
 * workspace and monaco slots where a plugin targets a family of names
 * (`mcp__*`, `memory_*`) instead of a literal list.
 */
export function matchesSpec(spec: ToolMatchSpec | undefined, value: string): boolean {
  if (!spec) return false;
  if (spec.exact && spec.exact.includes(value)) return true;
  if (spec.prefix && spec.prefix.some((p) => value.startsWith(p))) return true;
  if (spec.regex && spec.regex.test(value)) return true;
  return false;
}

/** Highest-priority entry of a slot whose matcher spec matches `value`. */
export function resolveMatched<T extends SlotEntry & { match?: ToolMatchSpec }>(
  slot: string,
  value: string,
): T | null {
  for (const entry of ext.entries<T>(slot)) {
    if (matchesSpec(entry.match, value)) return entry;
  }
  return null;
}

export interface ToolMatchSpec {
  exact?: string[];
  prefix?: string[];
  regex?: RegExp;
}
