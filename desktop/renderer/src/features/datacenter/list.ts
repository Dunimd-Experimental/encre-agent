/**
 * The data center's one list renderer.
 *
 * It emits the settings panels' list verbatim: the `.settings-card` frame
 * around a `.model-table`, whose cells are `.model-cell-name`,
 * `.model-cell-provider` and `.model-cell-actions` -- the same three cells the
 * model configuration, skill, command and MCP lists build. Column proportions
 * and typography are therefore shared, not re-derived: the leading cell is
 * 200px at the inherited 14px, the flexible cell is 13px secondary text, and
 * a trailing action cell is 120px right-aligned.
 *
 * Only the middle of a wide record table needs sizes the settings lists never
 * use, and those are classes (`dc--mid`, `dc--date`, `dc--flag`) -- never an
 * inline width.
 */

import { getIntlLocale, t } from "../i18n.js";
import type { ColumnDef, ColumnType, Row } from "./types.js";

export interface ListInput {
  columns: ColumnDef[];
  /** The rows for the current page. An empty selection is the caller's
   *  empty state, not this renderer's -- it never draws a header over
   *  nothing. */
  rows: Row[];
  /** Written onto every row as `data-<rowKeyAttr>` so the caller can bind clicks. */
  rowKeyAttr?: string;
  /** Which row property feeds that attribute. Defaults to the row's `id`. */
  rowKeyProp?: string;
}

/** Columns whose value is a Unix timestamp, keyed by column id. */
const EPOCH_SECONDS = new Set(["expires"]);
const EPOCH_MILLISECONDS = new Set(["visitTime", "dateAdded"]);

export function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** HTML-escape for use inside a double-quoted attribute value. */
export function escapeAttr(value: unknown): string {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

/** Locale-aware date; ISO when the environment has no usable locale. */
function formatDate(ms: number): string {
  try {
    return new Intl.DateTimeFormat(getIntlLocale(), { dateStyle: "short", timeStyle: "short" }).format(new Date(ms));
  } catch {
    return new Date(ms).toISOString();
  }
}

/** Format class for a cell whose rendering differs from plain text. */
function formatClass(type: ColumnType | undefined): string {
  return type && type !== "text" ? ` dc--${type}` : "";
}

/**
 * The cell's own classes, by position: the shared name cell leads every row,
 * a `w: "action"` cell trails it as the shared actions cell, and anything in
 * between is the shared flexible cell unless it declares a fixed track.
 *
 * A header's leading cell also carries `model-table-cell`, exactly as the
 * settings lists do -- that is what pins the header to the shared 13px label
 * size while the row's own name cell keeps the inherited 14px.
 */
function cellClasses(col: ColumnDef, first: boolean, header: boolean): string {
  const fmt = header ? "" : formatClass(col.t);
  if (first) return header ? "model-table-cell model-cell-name" : `model-cell-name${fmt}`;
  if (col.w === "action") return `model-table-cell model-cell-actions${fmt}`;
  if (col.w) return `model-table-cell dc--${col.w}${fmt}`;
  return `model-table-cell model-cell-provider${fmt}`;
}

/** Cells whose text can be clipped, and so are worth a hover title. */
function carriesLongText(col: ColumnDef): boolean {
  if (col.t === "html" || col.t === "bool" || col.t === "num") return false;
  return !EPOCH_SECONDS.has(col.k) && !EPOCH_MILLISECONDS.has(col.k);
}

function formatCell(row: Row, col: ColumnDef): string {
  const v = row[col.k];
  if (col.t === "html") return String(v ?? "");
  if (col.t === "bool") return v ? "✓" : "—";
  if (EPOCH_SECONDS.has(col.k)) return Number(v) ? formatDate(Number(v) * 1000) : "—";
  if (EPOCH_MILLISECONDS.has(col.k)) return Number(v) ? formatDate(Number(v)) : "—";
  return escapeHtml(v);
}

export function buildList(input: ListInput): string {
  const cols = input.columns;
  const head = cols.map((c, i) => cellClasses(c, i === 0, true));
  const cellCls = cols.map((c, i) => cellClasses(c, i === 0, false));

  const header = cols
    .map((c, i) => `<div class="${head[i]}">${escapeHtml(t(c.labelKey))}</div>`)
    .join("");

  const body = input.rows
    .map((row) => {
      const cells = cols
        .map((c, i) => {
          // A clipped cell stays readable in full through the app's own
          // tooltip -- `data-tooltip`, never the native `title`, which the
          // global tooltip has to strip and re-attach first.
          const tip = carriesLongText(c) ? ` data-tooltip="${escapeAttr(String(row[c.k] ?? ""))}"` : "";
          let content = formatCell(row, c);
          // The name cell is a flex row (glyph + label), and `text-overflow`
          // only works on a box -- so a plain-text label is wrapped.
          if (i === 0 && c.t !== "html") content = `<span>${content}</span>`;
          return `<div class="${cellCls[i]}"${tip}>${content}</div>`;
        })
        .join("");
      const keyAttr = input.rowKeyAttr
        ? ` data-${input.rowKeyAttr}="${escapeAttr(row[input.rowKeyProp ?? "id"] ?? "")}"`
        : "";
      return `<div class="model-table-row"${keyAttr}>${cells}</div>`;
    })
    .join("");

  return `<div class="settings-card"><div class="model-table dc-list"><div class="model-table-header">${header}</div>${body}</div></div>`;
}

/**
 * The pager under a record table. Nothing in the app paginated before, so
 * this is the whole widget: previous, `第 x / y 页`, next. It renders nothing
 * for a single page; `module.ts` owns the current page and re-renders.
 */
export function buildPager(page: number, pages: number): string {
  if (pages <= 1) return "";
  const btn = (target: string, icon: string, labelKey: string, enabled: boolean): string =>
    `<button class="btn-icon" data-page="${target}"${enabled ? "" : " disabled"} data-tooltip="${escapeAttr(t(labelKey))}"><i data-lucide="${icon}" class="lucide"></i></button>`;
  return `<div class="dc-pager">
    ${btn("prev", "chevron-left", "datacenter.prevPage", page > 1)}
    <span class="dc-pager-label">${escapeHtml(t("datacenter.page", { p: String(page), n: String(pages) }))}</span>
    ${btn("next", "chevron-right", "datacenter.nextPage", page < pages)}
  </div>`;
}
