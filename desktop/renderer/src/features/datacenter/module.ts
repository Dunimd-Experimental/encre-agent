/**
 * A module's page. Two shapes:
 *
 * - 运行日志 is the run-log table itself — the same view the Encre Agent
 *   Desktop `logs` page mounts (`ui/log_view.ts`), embedded here rather than
 *   reimplemented, so both surfaces stay one component.
 * - every other module is a record table built by `list.ts::buildList` from
 *   the rows core sends, paged, and narrowed by the filter dropdown below.
 *
 * Narrowing is a match, not a search: the automation history's 全部任务
 * pattern. The registry names one column per module (`ModuleMeta.filter`),
 * its distinct values become the options, and picking one keeps the rows
 * whose value equals it. There is no free-text box, and the dropdown is the
 * app's shared control (`ui/dropdown.ts`) rather than a data-center copy.
 *
 * When there is nothing to show, the page renders the app's centered empty
 * state and no table at all — a header over nothing is not an empty state.
 *
 * The breadcrumb is three levels deep (数据中心 / 浏览器 / Cookies) and every
 * level is clickable.
 */

import { t } from "../i18n.js";
import { renderCrumbs } from "../../ui/crumbs.js";
import { bindDropdownImpl, renderDropdownImpl, type DropdownOption } from "../../ui/dropdown.js";
import { mountLogView, type LogViewHandle } from "../../ui/log_view.js";
import { buildList, buildPager, escapeAttr, escapeHtml } from "./list.js";
import { MODULES, categoryById } from "./registry.js";
import type { ColumnDef, DataProvider, ModuleRows, Row } from "./types.js";

export interface ModulePageOptions {
  provider: DataProvider;
  categoryId: string;
  moduleId: string;
  /** Breadcrumb category level — back to the module list. */
  onBack(): void;
  /** Breadcrumb root — back to the home page. */
  onRoot(): void;
}

/** Records shown per page. */
const PAGE_SIZE = 50;

/** DOM ids of this page's filter dropdown, per `renderDropdownImpl`'s scheme. */
const FILTER_ID = "datacenter-filter";

/** Trailing action column a URL-bearing table gets, appended at render time
 *  so the record shape core sends stays the single source of truth. */
const urlActionCol: ColumnDef = {
  k: "__action",
  labelKey: "settings.actions",
  t: "html",
  w: "action",
};

/** The registry's columns, plus the open-in-window cell URL tables need. */
function columnsFor(metaColumns: ColumnDef[]): ColumnDef[] {
  return metaColumns.some((c) => c.k === "url") ? [...metaColumns, urlActionCol] : metaColumns;
}

export class ModulePage {
  readonly root: HTMLElement;
  readonly categoryId: string;
  readonly moduleId: string;
  private readonly opts: ModulePageOptions;
  private data: ModuleRows | null = null;
  /** Selected filter value; "" means 全部. */
  private filter = "";
  private page = 1;
  private logView: LogViewHandle | null = null;

  constructor(parent: HTMLElement, opts: ModulePageOptions) {
    this.opts = opts;
    this.categoryId = opts.categoryId;
    this.moduleId = opts.moduleId;
    this.root = document.createElement("div");
    this.root.className = "dc-page";
    parent.appendChild(this.root);
    this.renderSkeleton();

    // The run log is a live file the main process already parses and polls;
    // there is no record reader for it and there never needs to be one.
    if (opts.moduleId === "runlog") {
      this.logView = mountLogView(this.slot());
      return;
    }

    void this.opts.provider.fetchRows(opts.moduleId).then((data) => {
      this.data = data;
      this.renderFilter();
      this.renderBody();
    });
  }

  /** Release the page's listeners, timer and DOM. */
  destroy(): void {
    this.logView?.destroy();
    this.logView = null;
    this.root.remove();
  }

  private slot(): HTMLElement {
    return this.root.querySelector<HTMLElement>(".dc-mod-content") as HTMLElement;
  }

  private get meta() {
    return MODULES[this.opts.moduleId];
  }

  /** The column the filter matches on, when the module declares one. */
  private get filterKey(): string | null {
    const key = this.meta?.filter;
    if (!key || !this.meta?.columns?.some((c) => c.k === key)) return null;
    return key;
  }

  /** Static shell: breadcrumb bar (+ count and filter) + body slot. */
  private renderSkeleton(): void {
    const cat = categoryById(this.opts.categoryId);
    const meta = this.meta;
    // The run-log view brings its own header, batching and refresh, so the
    // table's count and filter would only sit there doing nothing.
    const isLog = this.opts.moduleId === "runlog";
    const bar = isLog
      ? ""
      : (this.filterKey ? `<span class="dc-filter-slot" id="${FILTER_ID}-slot"></span>` : "");
    this.root.innerHTML = `
      <div class="dc-page-bar">
        <div class="dc-crumb-slot"></div>
        ${bar}
      </div>
      <div class="dc-page-body">
        <div class="dc-mod-content"></div>
      </div>`;

    renderCrumbs(this.root.querySelector(".dc-crumb-slot") as HTMLElement, [
      { label: t("settings.dataCenter"), onActivate: () => this.opts.onRoot() },
      { label: cat ? t(cat.nameKey) : "", onActivate: () => this.opts.onBack() },
      { label: meta ? t(meta.labelKey) : "" },
    ]);

  }

  /**
   * Build the filter from the values the store actually holds, through the
   * app's shared dropdown. A column with one distinct value has nothing to
   * choose between, so the control is left out rather than shown disabled.
   */
  private renderFilter(): void {
    const key = this.filterKey;
    const slot = this.root.querySelector<HTMLElement>(".dc-filter-slot");
    if (!key || !slot || !this.data) return;

    const counts = new Map<string, number>();
    for (const row of this.data.rows) {
      const value = String(row[key] ?? "").trim();
      if (!value) continue;
      counts.set(value, (counts.get(value) ?? 0) + 1);
    }
    if (counts.size <= 1) {
      slot.innerHTML = "";
      return;
    }

    const options: DropdownOption[] = [
      { id: "", label: t("datacenter.filterAll") },
      ...[...counts.keys()]
        .sort((a, b) => (counts.get(b)! - counts.get(a)!) || a.localeCompare(b))
        .map((value) => ({ id: value, label: value })),
    ];

    const onChange = (val: string): void => {
      this.filter = val;
      this.page = 1;
      this.renderBody();
    };

    slot.innerHTML = renderDropdownImpl(FILTER_ID, options, this.filter, onChange);
    bindDropdownImpl(FILTER_ID, onChange);
    this.refreshIcons();
  }

  private filtered(): Row[] {
    if (!this.data) return [];
    const key = this.filterKey;
    if (!key || !this.filter) return this.data.rows;
    return this.data.rows.filter((r) => String(r[key] ?? "").trim() === this.filter);
  }

  /** Table, or the centered empty state when there are no rows to show. */
  private renderBody(): void {
    const meta = this.meta;
    const data = this.data;
    if (!meta?.columns || !data) {
      this.paintEmpty();
      return;
    }

    const rows = this.filtered();
    if (!rows.length) {
      this.paintEmpty();
      return;
    }
    this.renderTable(rows);
  }

  /**
   * The app's shared centered empty state (`market`, `agents`, `search` all
   * use it): one icon, one line, and no table underneath -- a header over
   * nothing is not an empty state. Every module shows the same one.
   */
  private paintEmpty(): void {
    this.slot().innerHTML = `<div class="si-empty-center">
      <i data-lucide="inbox" class="lucide"></i>
      <span class="si-empty-title">${escapeHtml(t("datacenter.statEmpty"))}</span>
    </div>`;
    this.refreshIcons();
  }

  private renderTable(rows: Row[]): void {
    const meta = this.meta;
    const data = this.data;
    const slot = this.slot();
    if (!meta?.columns || !data) return;

    // Page over everything core sent -- a long store is read a page at a
    // time, not cut off after an arbitrary number of rows.
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    if (this.page > pages) this.page = pages;
    if (this.page < 1) this.page = 1;
    const start = (this.page - 1) * PAGE_SIZE;

    const columns = columnsFor(meta.columns);
    const withActions = columns.some((c) => c.k === "__action");
    const visible = rows.slice(start, start + PAGE_SIZE).map((row) => {
      if (!withActions) return row;
      const url = String(row.url ?? "");
      const cell = url
        ? `<button class="btn-icon" data-action="open-url" data-url="${escapeAttr(url)}" data-tooltip="${escapeAttr(t("settings.openInBrowser"))}"><i data-lucide="arrow-up-right" class="lucide"></i></button>`
        : "";
      return { ...row, __action: cell };
    });

    // Core caps what it sends (_MAX_ROWS); say so rather than let the last
    // page look short for no reason.
    const omitted = data.truncated ? data.total - data.rows.length : 0;
    const note = omitted > 0
      ? `<div class="dc-grid-note">${escapeHtml(t("datacenter.statMore", { n: String(omitted) }))}</div>`
      : "";

    slot.innerHTML = `<div class="dc-list-wrap">${buildList({ columns, rows: visible })}${buildPager(this.page, pages)}${note}</div>`;

    this.refreshIcons();
    this.bindUrlActions(slot);
    this.bindPager(slot);
  }

  /** Pager clicks change the page and repaint the table in place. */
  private bindPager(scope: HTMLElement): void {
    scope.querySelectorAll<HTMLElement>("[data-page]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-page");
        const rows = this.filtered();
        const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
        const next = target === "prev" ? this.page - 1 : target === "next" ? this.page + 1 : this.page;
        if (next < 1 || next > pages || next === this.page) return;
        this.page = next;
        this.renderTable(rows);
        this.root.querySelector(".dc-page-body")?.scrollTo({ top: 0 });
      });
    });
  }

  /** Re-render lucide icons added by this page's render pass. */
  private refreshIcons(): void {
    const lucide = (window as unknown as { lucide?: { createIcons?: (o: { root: HTMLElement }) => void } }).lucide;
    lucide?.createIcons?.({ root: this.root });
  }

  /** Delegate clicks on the `arrow-up-right` cells to the ESD child window. */
  private bindUrlActions(scope: HTMLElement): void {
    scope.querySelectorAll<HTMLElement>("[data-action='open-url']").forEach((btn) => {
      btn.addEventListener("click", () => {
        const url = btn.getAttribute("data-url") || "";
        if (!url) return;
        const api = (window as unknown as { electronAPI?: { openChildWindow?: (u: string, label: string) => void } }).electronAPI;
        void api?.openChildWindow?.(url, url);
      });
    });
  }
}
