/**
 * The home page's donut chart.
 *
 * One slice per category. Hovering a slice makes it grow *and* extend: the
 * slice translates outward along its own bisector while scaling up, so it reads
 * as "变大变长" rather than a uniform zoom. Both numbers are baked into CSS
 * custom properties per slice because the direction differs per slice and CSS
 * cannot compute an angle.
 *
 * SVG has no `z-index`, so the hovered slice is re-appended to the end of the
 * group to sit on top of its neighbours.
 */

import type { CategoryCount } from "./types.js";

const CX = 170;
const CY = 170;
const R_OUT = 150;
const R_IN = 88;
/** Angular gap between slices, in radians. */
const GAP = 0.016;
/** How far a hovered slice slides outward, in viewBox units. */
const LIFT = 11;
/**
 * Minimum angular span per visible slice (~7°, expressed as a fraction of the
 * full circle). The real distribution can be extremely lopsided — on a live
 * install rollback snapshots hold 99% of all bytes — and a 0.3° sliver cannot
 * be hovered or clicked. Every visible slice gets at least this share; the
 * big slices yield proportionally. The *centre label* keeps showing the true
 * percentage, so the geometry stays usable while the numbers stay honest.
 *
 * Units: every span below is a FRACTION of the circle (0..1, they sum to 1).
 * They are only converted to radians at draw time (`span * TAU`). Mixing the
 * two is what once drew the whole pie inside a single radian.
 */
const MIN_SHARE = 0.02;
/** Just the circle constant, so the arithmetic reads cleanly. */
const TAU = Math.PI * 2;

function arcPath(r0: number, r1: number, a0: number, a1: number): string {
  const pt = (r: number, a: number): [number, number] => [
    CX + r * Math.cos(a),
    CY + r * Math.sin(a),
  ];
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = pt(r1, a0);
  const [x1, y1] = pt(r1, a1);
  const [x2, y2] = pt(r0, a1);
  const [x3, y3] = pt(r0, a0);
  return (
    `M${x0.toFixed(2)} ${y0.toFixed(2)}A${r1} ${r1} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}` +
    `L${x2.toFixed(2)} ${y2.toFixed(2)}A${r0} ${r0} 0 ${large} 0 ${x3.toFixed(2)} ${y3.toFixed(2)}Z`
  );
}

export interface PieCallbacks {
  /** Fired when the pointer enters/leaves a slice (null = nothing hovered). */
  onHover(id: string | null): void;
  /** Fired when a slice is clicked. */
  onSelect(id: string): void;
}

const SVG_NS = "http://www.w3.org/2000/svg";

export class PieChart {
  private readonly svg: SVGSVGElement;
  private readonly cb: PieCallbacks;
  private slices = new Map<string, SVGGElement>();
  private hovered: string | null = null;

  constructor(svg: SVGSVGElement, cb: PieCallbacks) {
    this.svg = svg;
    this.cb = cb;
  }

  /** Rebuild the slices. Cheap enough to call on every data refresh.
   *  Slice weight is bytes on disk — the figure the survey can observe —
   *  but each visible slice is guaranteed a minimum angular span so a
   *  dominant category cannot make the others unhoverable. */
  render(data: CategoryCount[]): void {
    const total = data.reduce((n, d) => n + d.bytes, 0) || 1;
    const visible = data.filter((d) => d.bytes > 0);
    // Redistribute spans: slices under MIN_SPAN are lifted to it, the rest
    // share what remains in proportion to their true share.
    const raw = visible.map((d) => d.bytes / total);
    const small = raw.map((f) => f < MIN_SHARE);
    // Small slices are lifted to MIN_SHARE; whatever is left of the circle
    // (1 - nSmall * MIN_SHARE) is shared by the large ones proportionally.
    const reserved = small.reduce((s, is) => s + (is ? MIN_SHARE : 0), 0);
    const largePool = raw.reduce((s, f, i) => s + (small[i] ? 0 : f), 0);
    const largeScale = largePool > 0 ? Math.max(0, 1 - reserved) / largePool : 0;
    const spans = raw.map((f, i) => (small[i] ? MIN_SHARE : f * largeScale));

    this.slices.clear();
    this.svg.textContent = "";

    let a = -Math.PI / 2; // start at 12 o'clock
    visible.forEach((d, i) => {
      const span = spans[i] * TAU;
      const g = Math.min(GAP, span / 6);
      const a0 = a + g;
      const a1 = a + span - g;
      a += span;

      const mid = (a0 + a1) / 2;
      const grp = document.createElementNS(SVG_NS, "g");
      grp.setAttribute("class", "dc-slice");
      grp.dataset.id = d.id;
      grp.style.setProperty("--dc-tx", `${(Math.cos(mid) * LIFT).toFixed(2)}px`);
      grp.style.setProperty("--dc-ty", `${(Math.sin(mid) * LIFT).toFixed(2)}px`);

      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", arcPath(R_IN, R_OUT, a0, a1));
      path.setAttribute("fill", d.color);
      grp.appendChild(path);

      grp.addEventListener("mouseenter", () => this.setHover(d.id, true));
      grp.addEventListener("mouseleave", () => this.setHover(null, true));
      grp.addEventListener("click", () => this.cb.onSelect(d.id));

      this.svg.appendChild(grp);
      this.slices.set(d.id, grp);
    });

    this.applyHover();
  }

  /**
   * Highlight a slice. Driven by the pie itself *and* by the category list, so
   * `emit` distinguishes "user touched the chart" from "something else set it".
   */
  setHover(id: string | null, emit = false): void {
    this.hovered = id;
    this.applyHover();
    if (emit) this.cb.onHover(id);
  }

  private applyHover(): void {
    const id = this.hovered;
    for (const [key, grp] of this.slices) {
      grp.classList.toggle("is-hover", key === id);
      grp.classList.toggle("is-dim", !!id && key !== id);
    }
    if (id) {
      const grp = this.slices.get(id);
      // SVG paint order == document order; move to the end to sit on top.
      if (grp) this.svg.appendChild(grp);
    }
  }
}
