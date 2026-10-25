#!/usr/bin/env node

/*
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
 */

/**
 * Reusable relationship-graph control.
 *
 * Renders a typed semantic graph (nodes + edges) as an SVG radial layout
 * with full interaction:
 *  - draggable nodes (incident edges follow);
 *  - canvas pan (drag the background) and wheel zoom (around the pointer);
 *  - theme-aware colours (reads --text / --border / --surface variables, so
 *    it renders correctly in both light and dark themes);
 *  - truncated node labels; type-coloured nodes; radius scaled by
 *    confidence; halo on hub nodes; dashed ring on skill candidates;
 *    dimmed styling on weakened nodes;
 *  - relation-styled edges (mentions / co_occurs / contradicts / ...);
 *  - a hover popover carrying the node's stats and its incident edges;
 *  - a click callback so the host can open a detail surface.
 *
 * The control owns no data fetching and no host-specific logic: the caller
 * passes plain `{nodes, edges}` and callbacks, so it is reusable for any
 * typed-graph surface.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

/** Node/edge payloads mirror the backend ``memory_graph`` frame. */
export interface MemoryGraphNode {
  id: string;
  type: string;
  title: string;
  preview: string;
  confidence: number;
  uses: number;
  updated: number;
  weakened: boolean;
  skill_candidate: boolean;
}

export interface MemoryGraphEdge {
  from: string;
  to: string;
  relation: string;
  weight: number;
}

export interface MemoryGraphData {
  nodes: MemoryGraphNode[];
  edges: MemoryGraphEdge[];
}

export interface MemoryGraphOptions {
  /** Invoked when a node is clicked (host opens the detail surface). */
  onSelect?: (node: MemoryGraphNode) => void;
  /** Node types treated as hubs (bigger, haloed). Default: entity. */
  hubTypes?: string[];
  /** Colour palette override, keyed by node type. */
  typeColors?: Record<string, string>;
  /** When set, user-dragged node positions persist in localStorage under
   *  this key and are re-applied on every new control instance — the layout
   *  a user arranged survives card close/reopen. */
  storageKey?: string;
}

interface Pos { x: number; y: number }

const DEFAULT_COLORS: Record<string, string> = {
  fact: "#6c8cff",
  preference: "#c084fc",
  lesson: "#f59e0b",
  decision: "#10b981",
  episode: "#64748b",
  entity: "#ec4899",
};

const EDGE_STYLE: Record<string, { color: string; width: number; dash: string }> = {
  mentions: { color: "#ec4899", width: 1.6, dash: "" },
  co_occurs: { color: "#94a3b8", width: 1.2, dash: "" },
  contradicts: { color: "#ef4444", width: 1.9, dash: "5 4" },
  causes: { color: "#ef4444", width: 1.4, dash: "2 3" },
  supersedes: { color: "#ef4444", width: 1.4, dash: "2 3" },
  supports: { color: "#22c55e", width: 1.5, dash: "" },
  refines: { color: "#22c55e", width: 1.2, dash: "2 3" },
};

const LABEL_MAX_CHARS = 16;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 3;

/** Node kind that acts as a pure index (statistical entity hub): no
 *  content behind it, so clicking opens nothing and hovering reveals the
 *  whole family instead. */
const HUB_TYPE = "entity";

const STYLE_ID = "memory-graph-style";

/** One-time stylesheet; theme-aware via the project's CSS custom properties.
 *  The bg and text custom properties are defined for [data-theme="dark"] at
 *  :root and overridden for [data-theme="light"], so both themes follow
 *  automatically. */
function ensureStyles(): void {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
.mem-graph-stage { position: relative; width: 100%; height: 100%; overflow: hidden; }
.mem-graph-empty {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 8px;
  color: var(--text-tertiary, #71717a); font-size: 13px; pointer-events: none;
}
.mem-graph-empty .mem-graph-empty-icon { font-size: 30px; opacity: 0.55; }
.mem-graph-stage > svg { width: 100%; height: 100%; display: block; cursor: grab; }
.mem-graph-stage.mg-panning > svg { cursor: grabbing; }
.mem-graph-stage.mg-dragging > svg { cursor: grabbing; }
.mem-graph-node { cursor: pointer; }
.mem-graph-node text {
  fill: var(--text-primary, #f5f5f7);
  paint-order: stroke;
  stroke: var(--bg-secondary, #111111);
  stroke-width: 3px;
  stroke-linejoin: round;
  font-weight: 500;
  pointer-events: none;
  user-select: none;
}
.mem-graph-node.faded { opacity: 0.12; }
.mem-graph-edge { transition: opacity 0.12s linear; }
/* Hover focus: edges not belonging to the hovered node fade out together
   with their (faded) endpoints, incident edges light up -- otherwise the
   canvas stays full of orphan-looking lines that connect invisible nodes. */
.mem-graph-edge.mg-dim { opacity: 0.05; }
.mem-graph-edge.mg-lit { opacity: 1; }
.mem-graph-popover {
  position: absolute; width: 280px; pointer-events: none; z-index: 30;
  background: var(--bg-tertiary, #1a1a1a); border: 1px solid var(--border-light, #262626);
  border-radius: 10px; padding: 12px 14px; color: var(--text-primary, #f5f5f7);
  font-size: 12px; font-family: inherit;
  box-shadow: var(--shadow-lg, 0 8px 30px rgba(0,0,0,0.25)); display: none;
}
.mem-graph-popover .mg-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.mem-graph-popover .mg-badge {
  font-size: 10px; font-weight: 700; letter-spacing: 0.4px; flex-shrink: 0;
  padding: 1.5px 7px; border-radius: 4px;
}
.mem-graph-popover .mg-title { font-weight: 700; font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mem-graph-popover .mg-kv { display: flex; gap: 12px; font-size: 11px; color: var(--text-secondary, #a1a1aa); margin-bottom: 8px; }
.mem-graph-popover .mg-kv b { font-weight: 600; color: var(--text-primary, #f5f5f7); }
.mem-graph-popover .mg-label { font-size: 10px; font-weight: 600; letter-spacing: 0.5px; color: var(--text-tertiary, #71717a); margin: 7px 0 5px; }
.mem-graph-popover .mg-preview { font-size: 11px; line-height: 1.5; color: var(--text-secondary, #a1a1aa); margin-bottom: 4px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.mem-graph-popover .mg-links { display: flex; flex-direction: column; gap: 4px; }
.mem-graph-popover .mg-link { display: flex; align-items: center; gap: 7px; font-size: 11px; }
.mem-graph-popover .mg-rel { font-size: 9.5px; font-weight: 700; padding: 0.5px 6px; border-radius: 3px; min-width: 66px; text-align: center; flex-shrink: 0; }
.mem-graph-popover .mg-link .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary, #a1a1aa); }
.mem-graph-popover .mg-foot { margin-top: 9px; font-size: 10px; color: var(--accent, #999999); text-align: center; }
`;
  document.head.appendChild(style);
}

function el(name: string, attrs: Record<string, string | number>, text?: string): SVGElement {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Stable pseudo-random jitter from an id, so layouts survive re-renders. */
function jitter(id: string, salt: string): number {
  let h = 2166136261;
  const key = id + salt;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) / 4294967295) * 2 - 1; // [-1, 1]
}

function truncate(text: string, max = LABEL_MAX_CHARS): string {
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

/* Dragged positions AND the pan/zoom view are stored under the host-provided
 * key in viewBox units (the viewBox is fixed, so the map is render-size
 * independent) — reopening the surface restores exactly the view the user
 * left, instead of a "graph shrank" reset. */
interface GraphStore {
  pos: Record<string, Pos>;
  view?: { tx: number; ty: number; k: number };
}

function loadStore(key: string): GraphStore {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return { pos: {} };
    const parsed = JSON.parse(raw) as GraphStore | Record<string, Pos>;
    // Legacy shape: a bare {id: {x,y}} position map.
    if (!("pos" in parsed)) return { pos: (parsed as Record<string, Pos>) ?? {} };
    const store = parsed as GraphStore;
    return { pos: store.pos && typeof store.pos === "object" ? store.pos : {}, view: store.view };
  } catch {
    return { pos: {} };
  }
}

function saveStore(key: string, userPos: Map<string, Pos>, view?: { tx: number; ty: number; k: number }): void {
  try {
    localStorage.setItem(key, JSON.stringify({ pos: Object.fromEntries(userPos), view }));
  } catch {
    /* quota / privacy mode: interaction still works for this session */
  }
}

export interface MemoryGraphHandle {
  /** Re-render with new data (layout is recomputed deterministically). */
  update(data: MemoryGraphData): void;
  /** Fade out nodes whose title/preview do not match the query. */
  highlight(query: string): void;
  /** Reset pan/zoom to the initial fit. */
  resetView(): void;
  /** True while a node drag or canvas pan is in flight. */
  isInteracting(): boolean;
  /** Remove the control from its container. */
  destroy(): void;
}

export function createMemoryGraph(
  container: HTMLElement,
  data: MemoryGraphData,
  options: MemoryGraphOptions = {},
): MemoryGraphHandle {
  ensureStyles();
  const typeColors = { ...DEFAULT_COLORS, ...(options.typeColors || {}) };
  const hubTypes = new Set(options.hubTypes || ["entity"]);

  const stage = document.createElement("div");
  stage.className = "mem-graph-stage";
  container.appendChild(stage);
  const svg = el("svg", { viewBox: "0 0 1000 680", preserveAspectRatio: "xMidYMid meet" }) as SVGSVGElement;
  stage.appendChild(svg);
  const root = el("g", {});
  svg.appendChild(root);
  const gEdges = el("g", {});
  const gNodes = el("g", {});
  root.appendChild(gEdges);
  root.appendChild(gNodes);

  const popover = document.createElement("div");
  popover.className = "mem-graph-popover";
  stage.appendChild(popover);

  /** Shown instead of a blank canvas when the payload carries no nodes. */
  function updateEmptyState(): void {
    let empty = stage.querySelector(".mem-graph-empty");
    if (data.nodes.length > 0) {
      empty?.remove();
      return;
    }
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "mem-graph-empty";
      empty.innerHTML = `<span class="mem-graph-empty-icon">◌</span><span class="mem-graph-empty-text"></span>`;
      (empty.querySelector(".mem-graph-empty-text") as HTMLElement).textContent = "No memory nodes yet";
      stage.appendChild(empty);
    }
  }

  const nodeIndex = new Map<string, MemoryGraphNode>();
  const posIndex = new Map<string, Pos>();
  /** User-dragged positions; survive re-renders and data updates. */
  const userPos = new Map<string, Pos>();
  const store = options.storageKey ? loadStore(options.storageKey) : { pos: {}, view: undefined };
  for (const [id, p] of Object.entries(store.pos)) {
    if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) userPos.set(id, { x: p.x, y: p.y });
  }
  const persist = () => {
    if (options.storageKey) saveStore(options.storageKey, userPos, { tx, ty, k });
  };
  /** Wheel fires in bursts; coalesce the writes. */
  let persistTimer = 0;
  const persistSoon = () => {
    if (!options.storageKey || persistTimer) return;
    persistTimer = window.setTimeout(() => {
      persistTimer = 0;
      persist();
    }, 300);
  };
  const nodeEls = new Map<string, SVGGElement>();
  const edgeEls: { edge: MemoryGraphEdge; line: SVGLineElement }[] = [];

  /* pan/zoom state (viewBox units) — restored from the store so a reopened
     canvas shows the exact view the user left. */
  let tx = store.view && Number.isFinite(store.view.tx) ? store.view.tx : 0;
  let ty = store.view && Number.isFinite(store.view.ty) ? store.view.ty : 0;
  let k = store.view && Number.isFinite(store.view.k) ? Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, store.view.k)) : 1;

  function applyTransform(): void {
    root.setAttribute("transform", `translate(${tx} ${ty}) scale(${k})`);
  }

  /** Client point -> viewBox coordinates (accounts for aspect-fit letterboxing). */
  function toViewBox(clientX: number, clientY: number): Pos {
    const ctm = svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const inv = ctm.inverse();
    const pt = svg.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    const p = pt.matrixTransform(inv);
    return { x: p.x, y: p.y };
  }

  function colorOf(n: MemoryGraphNode): string {
    return typeColors[n.type] || "#64748b";
  }

  function layout(): void {
    nodeIndex.clear();
    posIndex.clear();
    for (const n of data.nodes) nodeIndex.set(n.id, n);
    const hubs = data.nodes.filter((n) => hubTypes.has(n.type));
    const rest = data.nodes.filter((n) => !hubTypes.has(n.type));
    const cx = 500;
    const cy = 330;
    const hubR = hubs.length > 0 ? 120 : 0;
    hubs.forEach((n, i) => {
      const a = (i / Math.max(1, hubs.length)) * Math.PI * 2 - Math.PI / 2;
      posIndex.set(n.id, { x: cx + Math.cos(a) * hubR, y: cy + Math.sin(a) * hubR });
    });
    const rings = Math.max(1, Math.ceil(rest.length / 10));
    const perRing = Math.ceil(rest.length / rings);
    rest.forEach((n, i) => {
      const ring = Math.floor(i / perRing);
      const idx = i % perRing;
      const count = Math.min(perRing, rest.length - ring * perRing);
      const radius = 215 + ring * 95;
      const a = (idx / Math.max(1, count)) * Math.PI * 2 + ring * 0.55 + jitter(n.id, "a") * 0.14;
      posIndex.set(n.id, {
        x: cx + Math.cos(a) * radius + jitter(n.id, "x") * 16,
        y: cy + Math.sin(a) * radius * 0.8 + jitter(n.id, "y") * 16,
      });
    });
    // User-dragged positions win over the computed layout.
    for (const [id, saved] of userPos) {
      if (nodeIndex.has(id)) posIndex.set(id, { ...saved });
    }
  }

  function radiusOf(n: MemoryGraphNode): number {
    const base = hubTypes.has(n.type) ? 24 : 13;
    return base * (0.75 + Math.max(0, Math.min(1, n.confidence)) * 0.5);
  }

  function relColor(rel: string): string {
    return (EDGE_STYLE[rel] || { color: "#94a3b8" }).color;
  }

  function showPopover(node: MemoryGraphNode): void {
    // Always read the freshest entry: the DOM may outlive several data
    // updates (structural-diff update path), so the captured closure node
    // can carry a stale use count.
    const n = nodeIndex.get(node.id) || node;
    const outs = data.edges.filter((e) => e.from === n.id);
    const ins = data.edges.filter((e) => e.to === n.id);
    const rows = [
      ...outs.map((e) => ({ rel: e.relation, dir: "→", other: nodeIndex.get(e.to) })),
      ...ins.map((e) => ({ rel: e.relation, dir: "←", other: nodeIndex.get(e.from) })),
    ].filter((r) => r.other);
    const color = colorOf(n);
    popover.innerHTML = `
      <div class="mg-head"><span class="mg-badge" style="color:${color};border:1px solid ${color}55;background:${color}1a">${n.type}</span><span class="mg-title"></span></div>
      <div class="mg-kv"><span>置信 <b>${n.confidence.toFixed(2)}</b></span><span>召回 <b>${n.uses} 次</b></span>${n.skill_candidate ? `<span style="color:var(--success,#22c55e)"><b>技能候选</b></span>` : ""}</div>
      ${n.preview ? `<div class="mg-preview"></div>` : ""}
      ${rows.length ? `<div class="mg-label">关系边（${rows.length}）</div>
      <div class="mg-links">${(n.type === HUB_TYPE ? rows : rows.slice(0, 4)).map((r) => `
        <div class="mg-link"><span class="mg-rel" style="color:${relColor(r.rel)};background:${relColor(r.rel)}1a">${r.rel}</span><span style="opacity:0.55">${r.dir}</span><span class="t">${r.other?.title ?? ""}</span></div>`).join("")}
        ${n.type !== HUB_TYPE && rows.length > 4 ? `<div class="mg-link" style="opacity:0.5">… 共 ${rows.length} 条</div>` : ""}
      </div>` : ""}`;
    (popover.querySelector(".mg-title") as HTMLElement).textContent = n.title;
    const prev = popover.querySelector(".mg-preview") as HTMLElement | null;
    if (prev) prev.textContent = n.preview;
    popover.style.display = "block";
    positionPopover(n);
  }

  /** Park the popover on the side of the node that faces AWAY from the
   *  canvas centre: most neighbours sit towards the centre (radial layout),
   *  so anchoring the card outward keeps it from covering exactly the nodes
   *  the user is trying to look at.  Fixed position -- it does not follow
   *  the pointer around. */
  function positionPopover(n: MemoryGraphNode): void {
    const rect = stage.getBoundingClientRect();
    const ctm = svg.getScreenCTM();
    const p = posIndex.get(n.id);
    if (!ctm || !p) return;
    const pt = new DOMPoint(p.x, p.y).matrixTransform(ctm);
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    let dx = pt.x - cx;
    let dy = pt.y - cy;
    const len = Math.hypot(dx, dy) || 1;
    dx /= len;
    dy /= len;
    const pw = popover.offsetWidth || 280;
    const ph = popover.offsetHeight || 160;
    let x = pt.x + dx * 26 - pw / 2;
    let y = pt.y + dy * 26 - ph / 2;
    x = Math.max(4, Math.min(x, rect.width - pw - 8));
    y = Math.max(4, Math.min(y, rect.height - ph - 8));
    popover.style.left = `${x}px`;
    popover.style.top = `${y}px`;
  }

  function hidePopover(): void {
    popover.style.display = "none";
  }

  /** Re-position one node's circle/halo/rings/label and its incident edges. */
  function repositionNode(id: string): void {
    const p = posIndex.get(id);
    const n = nodeIndex.get(id);
    const g = nodeEls.get(id);
    if (!p || !n || !g) return;
    const r = radiusOf(n);
    g.querySelectorAll<"circle">("circle").forEach((c) => {
      c.setAttribute("cx", String(p.x));
      c.setAttribute("cy", String(p.y));
    });
    const label = g.querySelector("text");
    if (label) {
      label.setAttribute("x", String(p.x));
      label.setAttribute("y", String(p.y + r + 14));
    }
    for (const { edge, line } of edgeEls) {
      if (edge.from === id) {
        line.setAttribute("x1", String(p.x));
        line.setAttribute("y1", String(p.y));
      } else if (edge.to === id) {
        line.setAttribute("x2", String(p.x));
        line.setAttribute("y2", String(p.y));
      }
    }
  }

  function render(): void {
    gEdges.innerHTML = "";
    gNodes.innerHTML = "";
    edgeEls.length = 0;
    nodeEls.clear();
    hidePopover();
    updateEmptyState();

    for (const e of data.edges) {
      const a = posIndex.get(e.from);
      const b = posIndex.get(e.to);
      if (!a || !b) continue;
      const s = EDGE_STYLE[e.relation] || { color: "#94a3b8", width: 1.2, dash: "" };
      const line = el("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        stroke: s.color, "stroke-width": s.width,
        ...(s.dash ? { "stroke-dasharray": s.dash } : {}),
        opacity: 0.8,
      }) as SVGLineElement;
      line.classList.add("mem-graph-edge");
      gEdges.appendChild(line);
      edgeEls.push({ edge: e, line });
    }

    for (const n of data.nodes) {
      const p = posIndex.get(n.id);
      if (!p) continue;
      const color = colorOf(n);
      const r = radiusOf(n);
      const g = el("g", { class: "mem-graph-node", "data-id": n.id }) as SVGGElement;

      if (hubTypes.has(n.type)) {
        g.appendChild(el("circle", {
          cx: p.x, cy: p.y, r: r + 9, fill: "none",
          stroke: color, "stroke-opacity": 0.3,
        }));
      }
      if (n.skill_candidate) {
        g.appendChild(el("circle", {
          cx: p.x, cy: p.y, r: r + 4, fill: "none",
          stroke: "#22c55e", "stroke-dasharray": "3 3", "stroke-width": 1.2,
        }));
      }
      g.appendChild(el("circle", {
        cx: p.x, cy: p.y, r,
        fill: hubTypes.has(n.type) ? `${color}26` : `${color}33`,
        stroke: color,
        "stroke-width": n.weakened ? 1 : 1.8,
        "stroke-opacity": n.weakened ? 0.45 : 1,
      }));
      g.appendChild(el("text", {
        x: p.x, y: p.y + r + 14, "text-anchor": "middle", "font-size": 10.5,
      }, truncate(n.title)));

      g.addEventListener("mouseenter", () => {
        if (dragState || panState) return;
        showPopover(n);
        // Hovering any node spotlights its family: neighbours stay lit, the
        // rest of the canvas fades, everything is restored on leave.  For
        // entity hubs (pure index nodes) this IS the whole interaction --
        // the family is what they exist to show.
        const family = new Set<string>([n.id]);
        data.edges.forEach((e) => {
          if (e.from === n.id) family.add(e.to);
          if (e.to === n.id) family.add(e.from);
        });
        nodeEls.forEach((ng, id) => ng.classList.toggle("faded", !family.has(id)));
        // Edges follow the same focus: only the hovered node's own edges
        // stay visible (lit and slightly thicker), every other edge fades
        // along with its endpoints -- otherwise the canvas stays full of
        // orphan-looking lines that connect already-invisible nodes, which
        // reads as "these lines connect nothing".
        edgeEls.forEach(({ edge, line }) => {
          const incident = edge.from === n.id || edge.to === n.id;
          line.classList.toggle("mg-dim", !incident);
          line.classList.toggle("mg-lit", incident);
          const base = (EDGE_STYLE[edge.relation] || { width: 1.2 }).width;
          line.setAttribute("stroke-width", String(incident ? base + 0.7 : base));
        });
      });
      g.addEventListener("mouseleave", () => {
        hidePopover();
        nodeEls.forEach((ng) => ng.classList.remove("faded"));
        edgeEls.forEach(({ edge, line }) => {
          line.classList.remove("mg-dim", "mg-lit");
          line.setAttribute("stroke-width", String((EDGE_STYLE[edge.relation] || { width: 1.2 }).width));
        });
      });
      g.addEventListener("click", (ev) => {
        if (suppressClick) {
          ev.stopImmediatePropagation();
          suppressClick = false;
          return;
        }
        options.onSelect?.(n);
      });
      g.addEventListener("pointerdown", (ev) => startNodeDrag(ev as PointerEvent, n));
      gNodes.appendChild(g);
      nodeEls.set(n.id, g);
    }
    applyTransform();
  }

  /* ── node dragging ─────────────────────────────────────────────────── */
  interface DragState {
    id: string;
    startX: number;
    startY: number;
    orig: Pos;
    moved: boolean;
  }
  let dragState: DragState | null = null;
  let suppressClick = false;

  function startNodeDrag(ev: PointerEvent, n: MemoryGraphNode): void {
    if (ev.button !== 0) return;
    const orig = posIndex.get(n.id);
    if (!orig) return;
    ev.preventDefault();
    ev.stopPropagation();
    dragState = { id: n.id, startX: ev.clientX, startY: ev.clientY, orig: { ...orig }, moved: false };
    stage.classList.add("mg-dragging");
    hidePopover();
    const g = nodeEls.get(n.id);
    try {
      g?.setPointerCapture(ev.pointerId);
    } catch {
      /* pointer capture is best-effort */
    }

    const onMove = (me: PointerEvent): void => {
      if (!dragState) return;
      const dx = me.clientX - dragState.startX;
      const dy = me.clientY - dragState.startY;
      if (!dragState.moved && Math.hypot(dx, dy) < 4) return;
      dragState.moved = true;
      const ctm = svg.getScreenCTM();
      const scale = ctm ? ctm.a : 1; // client px -> viewBox units
      const p = posIndex.get(dragState.id);
      if (!p) return;
      p.x = dragState.orig.x + dx / (scale * k);
      p.y = dragState.orig.y + dy / (scale * k);
      userPos.set(dragState.id, { ...p });
      repositionNode(dragState.id);
    };
    const onUp = (): void => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      try {
        g?.releasePointerCapture(ev.pointerId);
      } catch {
        /* already released */
      }
      stage.classList.remove("mg-dragging");
      if (dragState?.moved) {
        suppressClick = true;
        persist();
      }
      dragState = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  /* ── canvas pan + wheel zoom ───────────────────────────────────────── */
  interface PanState { startX: number; startY: number; origTx: number; origTy: number }
  let panState: PanState | null = null;

  svg.addEventListener("pointerdown", (ev) => {
    const target = ev.target as Element;
    if (target.closest(".mem-graph-node")) return; // node drag owns this
    if (ev.button !== 0) return;
    panState = { startX: ev.clientX, startY: ev.clientY, origTx: tx, origTy: ty };
    stage.classList.add("mg-panning");
    hidePopover();
  });
  window.addEventListener("pointermove", (ev) => {
    if (!panState) return;
    const ctm = svg.getScreenCTM();
    const scale = ctm ? ctm.a : 1; // client px -> viewBox units
    tx = panState.origTx + (ev.clientX - panState.startX) / scale;
    ty = panState.origTy + (ev.clientY - panState.startY) / scale;
    applyTransform();
  });
  window.addEventListener("pointerup", () => {
    if (!panState) return;
    panState = null;
    stage.classList.remove("mg-panning");
    persist();
  });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.1 : 0.9;
    const k2 = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, k * factor));
    if (k2 === k) return;
    const v = toViewBox(ev.clientX, ev.clientY);
    // keep the point under the cursor fixed: v = t + k*u
    tx = v.x - ((v.x - tx) * k2) / k;
    ty = v.y - ((v.y - ty) * k2) / k;
    k = k2;
    applyTransform();
    persistSoon();
  }, { passive: false });

  /** Structural signature of the current graph: node ids plus the full edge
   *  list.  Two payloads with the same signature render identically, so the
   *  update path can skip the DOM entirely (no flicker). */
  function sigOf(g: MemoryGraphData): string {
    return (
      g.nodes.map((n) => n.id).join(",") +
      "|" +
      g.edges.map((e) => `${e.from}>${e.to}:${e.relation}:${e.weight}`).join(",")
    );
  }

  function currentSig(): string {
    return sigOf(data);
  }

  layout();
  render();

  return {
    update(next: MemoryGraphData): void {
      const structuralChange = currentSig() !== sigOf(next);
      data = next;
      // Refresh the id→node index even when the DOM is kept: hover popovers
      // read through it, so they must show the fresh payload values.
      nodeIndex.clear();
      for (const n of next.nodes) nodeIndex.set(n.id, n);
      if (structuralChange) {
        // Keep pan/zoom (and dragged node positions via layout()'s userPos):
        // data refreshes must not yank the viewport back to origin.
        layout();
        render();
      }
      applyTransform();
    },
    highlight(query: string): void {
      const q = query.trim().toLowerCase();
      svg.querySelectorAll<SVGGElement>(".mem-graph-node").forEach((g) => {
        const n = nodeIndex.get(g.getAttribute("data-id") || "");
        const hit = !q || !n ||
          `${n.title} ${n.preview}`.toLowerCase().includes(q);
        g.classList.toggle("faded", !hit);
      });
    },
    resetView(): void {
      tx = 0; ty = 0; k = 1;
      applyTransform();
    },
    isInteracting(): boolean {
      return dragState !== null || panState !== null;
    },
    destroy(): void {
      hidePopover();
      stage.remove();
    },
  };
}
