/**
 * A category's page: the modules inside one category, drilled into from the
 * home page. Navigation grammar matches the automation sub-pages: the
 * container swaps wholesale, the breadcrumb's root is the way back.
 *
 * Each row is a plain `.si-home-card` — icon plus name — mirroring the four
 * category tiles on the home page. Nothing else: no file counts, no path
 * strings, no empty-state labels. The rows exist to be clicked; the details
 * live behind them. A module's *records* are the table (see `module.ts`).
 */

import { t } from "../i18n.js";
import { renderCrumbs } from "../../ui/crumbs.js";
import { MODULES, categoryById } from "./registry.js";
import { guardRender } from "../../core/ui_boundary.js";
import type { DataProvider } from "./types.js";

export interface CategoryPageOptions {
  provider: DataProvider;
  categoryId: string;
  /** Breadcrumb root — returns to the home page. */
  onBack(): void;
  /** Activating a module. */
  onOpenModule(categoryId: string, moduleId: string): void;
}

export class CategoryPage {
  readonly root: HTMLElement;
  readonly categoryId: string;
  private readonly opts: CategoryPageOptions;

  constructor(parent: HTMLElement, opts: CategoryPageOptions) {
    this.opts = opts;
    this.categoryId = opts.categoryId;
    this.root = document.createElement("div");
    this.root.className = "dc-page";
    parent.appendChild(this.root);
    this.render();
  }

  /** Rebuild the DOM in place — called on locale change and data refresh. */
  render(): void {
    // Same unified boundary as every other application surface.
    guardRender("datacenter", `category:${this.categoryId}`, () => this._renderImpl());
  }

  private _renderImpl(): void {
    const cat = categoryById(this.opts.categoryId);
    const items = cat?.items ?? [];

    const rows = items
      .map((id) => {
        const meta = MODULES[id];
        if (!meta) return "";
        // Reuse the sidebar home card (`.si-home-card`) so the module list
        // looks like the four category tiles above it — same background,
        // border, radius, hover, arrow animation. Only the row layout is
        // data-center-specific; see `.dc-mod-row.si-home-card` in styles.css.
        return `
        <button type="button" class="dc-mod-row si-home-card" data-mod="${id}">
          <span class="dc-mod-ico"><i data-lucide="${meta.icon}" class="lucide si-home-icon"></i></span>
          <span class="dc-mod-name si-home-label">${t(meta.labelKey)}</span>
          <i data-lucide="chevron-right" class="lucide si-home-arrow dc-cat-go"></i>
        </button>`;
      })
      .join("");

    this.root.innerHTML = `
      <div class="dc-page-bar"></div>
      <div class="dc-page-body">
        <div class="dc-mod-list" role="list">${rows}</div>
      </div>`;

    renderCrumbs(this.root.querySelector(".dc-page-bar") as HTMLElement, [
      { label: t("settings.dataCenter"), onActivate: () => this.opts.onBack() },
      { label: cat ? t(cat.nameKey) : "" },
    ]);

    for (const row of Array.from(this.root.querySelectorAll<HTMLButtonElement>(".dc-mod-row"))) {
      row.addEventListener("click", () => {
        const id = row.dataset.mod || "";
        if (id) this.opts.onOpenModule(this.opts.categoryId, id);
      });
    }
  }
}
