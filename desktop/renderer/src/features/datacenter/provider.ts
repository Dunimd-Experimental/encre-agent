/**
 * What data *does* exist right now — served by core.
 *
 * The pages never read data directly; they ask a {@link DataProvider}, and
 * this implementation asks the **core server** (`GET /datacenter/survey`).
 * All scanning and statistics live in `core/encre/server/datacenter.py`:
 * the renderer only formats what core reports, and never touches the
 * filesystem itself.
 *
 * Until `load()` resolves the getters return zeros — the page mounts
 * immediately and repaints once the survey lands. If core is unreachable
 * (backend still booting, dev-server preview) the page stays at zero
 * rather than inventing numbers.
 */

import { CATEGORIES } from "./registry.js";
import type { CategoryCount, DataProvider, ModuleRows, ModuleStat, Row } from "./types.js";

interface SurveyModule {
  id: string;
  files: number;
  bytes: number;
}

interface SurveyCategory {
  id: string;
  files: number;
  bytes: number;
  modules: SurveyModule[];
}

interface Survey {
  root: string;
  total_bytes: number;
  categories: SurveyCategory[];
}

/** Admin-channel requests are proxied through the main process (see
 *  `adminFetch` in main.ts): a file:// renderer cannot fetch the local admin
 *  HTTP channel directly because it sends no CORS headers by design. */

export class CoreProvider implements DataProvider {
  private filesByCategory = new Map<string, number>();
  private bytesByCategory = new Map<string, number>();
  private filesByModule = new Map<string, number>();
  private bytesByModule = new Map<string, number>();
  /** Root reported by core; empty until the first successful survey. */
  root = "";

  async load(): Promise<boolean> {
    let data: Survey;
    try {
      const res = await window.electronAPI?.adminFetch("/datacenter/survey");
      if (!res?.ok || !res.body) return false;
      data = JSON.parse(res.body) as Survey;
    } catch {
      return false;
    }
    this.apply(data);
    return true;
  }

  private apply(data: Survey): void {
    this.root = data.root || "";
    this.filesByCategory.clear();
    this.bytesByCategory.clear();
    this.filesByModule.clear();
    this.bytesByModule.clear();
    for (const cat of data.categories || []) {
      this.filesByCategory.set(cat.id, cat.files);
      this.bytesByCategory.set(cat.id, cat.bytes);
      for (const m of cat.modules || []) {
        this.filesByModule.set(m.id, m.files);
        this.bytesByModule.set(m.id, m.bytes);
      }
    }
  }

  countsByCategory(): CategoryCount[] {
    return CATEGORIES.map((c) => ({
      id: c.id,
      nameKey: c.nameKey,
      color: c.color,
      n: this.filesByCategory.get(c.id) ?? 0,
      bytes: this.bytesByCategory.get(c.id) ?? 0,
    }));
  }

  countOf(moduleId: string): number {
    return this.filesByModule.get(moduleId) ?? 0;
  }

  statOf(moduleId: string): ModuleStat {
    return {
      files: this.filesByModule.get(moduleId) ?? 0,
      bytes: this.bytesByModule.get(moduleId) ?? 0,
    };
  }

  /**
   * Module records, fetched live. Everything on disk comes from core; the one
   * store the Electron main process owns outright -- site permissions, which
   * are session state in memory and never written to the data root -- is read
   * from there instead, so the page does not have to know which source a
   * module came from.
   */
  async fetchRows(moduleId: string): Promise<ModuleRows> {
    if (moduleId === "perms") {
      try {
        const grants = await window.electronAPI?.listPermissionGrants?.() ?? [];
        const rows: Row[] = grants.map((g) => ({ id: `${g.origin}|${g.permission}`, ...g }));
        return { rows, total: rows.length, truncated: false };
      } catch {
        return { rows: [], total: 0, truncated: false };
      }
    }
    try {
      const res = await window.electronAPI?.adminFetch(`/datacenter/module?id=${encodeURIComponent(moduleId)}`);
      if (!res?.ok || !res.body) return { rows: [], total: 0, truncated: false };
      const data = JSON.parse(res.body) as { rows?: Row[]; total?: number; truncated?: boolean };
      return {
        rows: Array.isArray(data.rows) ? data.rows : [],
        total: data.total ?? 0,
        truncated: !!data.truncated,
      };
    } catch {
      return { rows: [], total: 0, truncated: false };
    }
  }

  total(): number {
    let sum = 0;
    for (const v of this.filesByCategory.values()) sum += v;
    return sum;
  }
}
