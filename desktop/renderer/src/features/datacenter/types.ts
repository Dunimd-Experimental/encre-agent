/**
 * Data-center domain types.
 *
 * The data center is a first-class Encre Agent Desktop Subwindow (ESD) page (same tier as the browser tab), not
 * a webview. It is split into halves that must stay separate:
 *
 * - **registry** (`registry.ts`) — what data *can* exist: the four categories and
 *   (later) the modules inside them. Static, hand-maintained schema.
 * - **provider** (`provider.ts`) — what data *does* exist right now: per-module
 *   record counts and rows. Backed by a stub until the backend serves the real
 *   data tree, so no page imports data directly.
 *
 * Every user-visible string in the registry is an **i18n key**, never a literal:
 * the schema is static but the labels are not.
 */

/**
 * How a cell is rendered. Each value maps onto one `.dc--<type>` class in
 * styles.css -- see `list.ts::formatClass`.
 *
 * `html` is the escape hatch for cells the page builds itself (a lucide icon,
 * an action button); its content is written by the renderer, never by a
 * record from core.
 */
export type ColumnType =
  | "text"
  | "strong"
  | "mono"
  | "num"
  | "secret"
  | "bool"
  | "html";

/**
 * Track for a *middle* column. The leading column is always the shared name
 * cell and a trailing `action` column is always the shared actions cell, so
 * only the columns in between need a size; leave it unset to get the shared
 * flexible cell.
 *
 * Tracks are definite so a header cell and its body cell cannot drift apart.
 */
export type ColumnWidth = "mid" | "date" | "flag" | "action";

export interface ColumnDef {
  /** Key into {@link Row}. */
  k: string;
  /** i18n key for the column header. */
  labelKey: string;
  /** Cell rendering class. Defaults to plain text. */
  t?: ColumnType;
  /** Grid track class. Defaults to `mid`. */
  w?: ColumnWidth;
}

export interface Row {
  id: string;
  [key: string]: unknown;
}

export interface CategoryDef {
  id: string;
  /** i18n key for the category name. */
  nameKey: string;
  /** i18n key for the one-line description under the name. */
  noteKey: string;
  /** Lucide icon name, kebab-case. */
  icon: string;
  /** Pie slice fill + the category tile's icon-chip background. */
  color: string;
  /** Icon colour drawn on top of {@link color}. */
  fg: string;
  /** Module ids, in display order. */
  items: string[];
}

/** One row of the home page's category list (= one pie slice). */
export interface CategoryCount {
  id: string;
  nameKey: string;
  color: string;
  /** File count for the category (directory stores count their entries). */
  n: number;
  /** Bytes on disk — the pie's slice weight. */
  bytes: number;
}

/** Real statistics for one module, as reported by core's survey. */
export interface ModuleStat {
  files: number;
  bytes: number;
}

/** One module's records, fetched from core. */
export interface ModuleRows {
  rows: Row[];
  /** Total record count on the backend (may exceed `rows.length`). */
  total: number;
  /** True when the backend capped what it sent. */
  truncated: boolean;
}

/** Static metadata for one module (category pages render from this). */
export interface ModuleMeta {
  id: string;
  /** i18n key for the module name. */
  labelKey: string;
  /** Lucide icon name, kebab-case. */
  icon: string;
  /** Set when the module has a table view backed by a core row reader. */
  columns?: ColumnDef[];
  /**
   * Column whose distinct values become the module page's filter options —
   * the automation history's 全部任务 pattern: pick a value, match rows,
   * instead of typing a substring. Must name one of `columns`.
   */
  filter?: string;
}

/**
 * Supplies the dynamic half of the page. The numbers come from the core
 * server's `GET /datacenter/survey` endpoint; `load()` must be awaited
 * once before the synchronous getters return real values.
 */
export interface DataProvider {
  /** Fetch the survey from core. Resolves false when core is unreachable. */
  load(): Promise<boolean>;
  /** The data root core reported, e.g. `~/.dunimd/encre`. Empty pre-survey. */
  readonly root: string;
  /** Per-category statistics, in `CATEGORIES` order. */
  countsByCategory(): CategoryCount[];
  /** File count for one module. */
  countOf(moduleId: string): number;
  /** Files + bytes for one module. Zeros until the survey lands. */
  statOf(moduleId: string): ModuleStat;
  /** Records for one module, fetched live from core. */
  fetchRows(moduleId: string): Promise<ModuleRows>;
  /** File count across every module. */
  total(): number;
}
