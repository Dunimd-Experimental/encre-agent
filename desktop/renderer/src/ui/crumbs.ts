/**
 * Breadcrumb chips — the `session-crumb` grammar shared by the session bar's
 * sub-agent trail and any in-app drill-in page (data center, …).
 *
 * Levels render left to right: every level that carries an `onActivate`
 * becomes a clickable `.session-crumb` button (the first one also gets
 * `session-crumb-root`), the tail level without a callback renders as the
 * bold `.session-crumb-current` "you are here" span. Separators are the
 * usual `/`. Full labels land in `data-tooltip` (global tooltip); the
 * visible text is truncated to keep long names from blowing the bar out.
 */

export interface CrumbLevel {
  label: string;
  /** Absent = the tail "current" level (rendered non-clickable). */
  onActivate?: () => void;
}

/** Visible-text cap, matching the session bar's crumb truncation. */
const TRUNCATE_MAX = 18;

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function truncate(s: string, max = TRUNCATE_MAX): string {
  return s.length > max ? s.slice(0, max) + "…" : s;
}

/** Replace *container*'s content with the crumb trail for *levels*. */
export function renderCrumbs(container: HTMLElement, levels: CrumbLevel[]): void {
  const html = levels
    .map((lvl, i) => {
      const sep = i > 0 ? `<span class="session-crumb-sep">/</span>` : "";
      const tip = ` data-tooltip="${esc(lvl.label)}"`;
      const text = esc(truncate(lvl.label));
      const node = lvl.onActivate
        ? `<button type="button" class="session-crumb${i === 0 ? " session-crumb-root" : ""}"${tip}>${text}</button>`
        : `<span class="session-crumb-current"${tip}>${text}</span>`;
      return sep + node;
    })
    .join("");
  // A streaming sub-agent re-runs the session-bar subscriber ~12x/second.
  // Replacing innerHTML every time swaps the crumb button out from under an
  // in-flight click (mousedown lands on the old node, mouseup on the fresh
  // one -> the browser fires no click), which is why breadcrumb "back"
  // sometimes needs a second click. Unchanged markup keeps the DOM -- and
  // its listeners -- intact.
  if (container.innerHTML === html) return;
  container.innerHTML = html;

  const buttons = container.querySelectorAll<HTMLButtonElement>("button.session-crumb");
  let bi = 0;
  for (const lvl of levels) {
    if (!lvl.onActivate) continue;
    buttons[bi++]?.addEventListener("click", lvl.onActivate);
  }
}
