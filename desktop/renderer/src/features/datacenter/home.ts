/**
 * The data center's landing page: the four categories on the left, the donut
 * on the right, centred together as one composition. Nothing else — no page
 * title, no totals, no hint text.
 *
 * Every number shown comes from the {@link DataProvider} (core's
 * `/datacenter/survey`): tiles show real file counts and bytes, the chart's
 * slices are weighted by bytes on disk, and hovering reveals the same
 * figures in the centre. If the survey has not landed yet the page renders
 * at zero and repaints when it does.
 *
 * The category tiles double as the chart's legend: each tile's icon chip is
 * painted in the same hue as its slice, so no separate legend is needed.
 * Hovering either one highlights both, which is why the hover state lives
 * here and is pushed into {@link PieChart} rather than being driven by
 * `:hover`.
 */

import { t } from "../i18n.js";
import { PieChart } from "./pie.js";
import { CATEGORIES } from "./registry.js";
import type { CategoryCount, DataProvider } from "./types.js";

export interface HomeOptions {
  provider: DataProvider;
  /** Fired when a category tile or a slice is activated. */
  onOpen(categoryId: string): void;
}

/** Human-readable size, e.g. `1.2 GB` / `0 B`. Shared shape with the tray. */
export function formatBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export class HomeView {
  private readonly root: HTMLElement;
  private readonly opts: HomeOptions;
  private readonly pie: PieChart;

  private tiles = new Map<string, HTMLButtonElement>();
  private counts: CategoryCount[] = [];

  private centerName!: HTMLElement;
  private centerNum!: HTMLElement;
  private centerUnit!: HTMLElement;

  private hovered: string | null = null;

  constructor(parent: HTMLElement, opts: HomeOptions) {
    // Own wrapper, appended to the tab's container — the container belongs to
    // `index.ts` and must survive page swaps (destroy() used to remove it,
    // taking the whole data center with it).
    this.root = document.createElement("div");
    this.root.className = "dc-home";
    parent.appendChild(this.root);
    this.opts = opts;
    this.root.innerHTML = `
      <div class="dc-home-main">
        <div class="dc-cat-col" role="list"></div>
        <div class="dc-chart-col">
          <div class="dc-pie-box">
            <svg class="dc-pie" viewBox="0 0 340 340" role="img"></svg>
            <div class="dc-pie-center">
              <div class="dc-pc-name"></div>
              <div class="dc-pc-num"></div>
              <div class="dc-pc-unit"></div>
            </div>
          </div>
        </div>
      </div>`;

    this.centerName = this.root.querySelector(".dc-pc-name") as HTMLElement;
    this.centerNum = this.root.querySelector(".dc-pc-num") as HTMLElement;
    this.centerUnit = this.root.querySelector(".dc-pc-unit") as HTMLElement;

    this.buildTiles();

    this.pie = new PieChart(this.root.querySelector(".dc-pie") as SVGSVGElement, {
      onHover: (id) => this.applyHover(id),
      onSelect: (id) => this.opts.onOpen(id),
    });
  }

  /** Remove the view's DOM (navigation swaps whole pages). */
  destroy(): void {
    this.root.remove();
  }

  /**
   * Re-read counts from the provider and repaint the list + chart.
   *
   * Tiles are rebuilt rather than patched: the labels come from `t()`, so a
   * locale change has to re-run them, and four nodes are cheap.
   */
  refresh(): void {
    this.counts = this.opts.provider.countsByCategory();
    this.buildTiles();
    this.pie.render(this.counts);
    this.applyHover(this.hovered);
  }

  private buildTiles(): void {
    const col = this.root.querySelector(".dc-cat-col") as HTMLElement;
    col.textContent = "";
    this.tiles.clear();
    for (const cat of CATEGORIES) {
      const c = this.counts.find((x) => x.id === cat.id);
      const meta = `${t("datacenter.statModules", { n: String(cat.items.length) })} · ${formatBytes(c?.bytes ?? 0)}`;
      const tile = document.createElement("button");
      tile.type = "button";
      // Reuse the project's sidebar home card (`.si-home-card`) so the four
      // data-center tiles share the exact same UI as the session inner home.
      // The data-center overrides only the row layout and arrow visibility.
      tile.className = "dc-cat si-home-card";
      tile.dataset.id = cat.id;
      tile.setAttribute("role", "listitem");
      tile.innerHTML = `
        <span class="dc-cat-ico" style="--dc-cat-color:${cat.color}">
          <i data-lucide="${cat.icon}" class="lucide si-home-icon"></i>
        </span>
        <span class="dc-cat-txt">
          <span class="dc-cat-name si-home-label">${t(cat.nameKey)}</span>
          <span class="dc-cat-meta si-home-desc">${meta}</span>
        </span>
        <i data-lucide="chevron-right" class="lucide si-home-arrow dc-cat-go"></i>`;

      tile.addEventListener("click", () => this.opts.onOpen(cat.id));
      tile.addEventListener("mouseenter", () => this.setHover(cat.id));
      tile.addEventListener("mouseleave", () => this.setHover(null));
      tile.addEventListener("focus", () => this.setHover(cat.id));
      tile.addEventListener("blur", () => this.setHover(null));

      col.appendChild(tile);
      this.tiles.set(cat.id, tile);
    }
  }

  /** Set the shared hover state (tile highlight + slice lift + centre label). */
  setHover(id: string | null): void {
    this.hovered = id;
    this.pie.setHover(id);
    this.applyHover(id);
  }

  private applyHover(id: string | null): void {
    this.hovered = id;
    for (const [key, tile] of this.tiles) tile.classList.toggle("is-hover", key === id);

    // Default state: the grand total across every category. Hovering a slice
    // or a tile swaps it for that category's figures; leaving restores this.
    if (!id) {
      const bytes = this.counts.reduce((sum, x) => sum + x.bytes, 0);
      const files = this.counts.reduce((sum, x) => sum + x.n, 0);
      this.centerName.textContent = t("datacenter.total");
      this.centerNum.textContent = formatBytes(bytes);
      this.centerUnit.textContent = files
        ? t("datacenter.statFiles", { n: String(files) })
        : t("datacenter.statEmpty");
      return;
    }

    const cat = CATEGORIES.find((c) => c.id === id);
    const c = this.counts.find((x) => x.id === id);
    const bytes = c?.bytes ?? 0;
    const total = this.counts.reduce((sum, x) => sum + x.bytes, 0) || 1;

    this.centerName.textContent = cat ? t(cat.nameKey) : "";
    this.centerNum.textContent = formatBytes(bytes);
    this.centerUnit.textContent = t("datacenter.percent", { p: ((bytes / total) * 100).toFixed(1) });
  }
}
